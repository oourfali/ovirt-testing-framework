#
# Copyright 2014 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
import ConfigParser
import functools
import logging
import os

import lockfile
import nose.core
import nose.config
from ovirtsdk.infrastructure.errors import RequestError

import testenv

import merge_repos
import repoverify
import paths
import testlib
import utils
import virt


def _with_repo_server(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with utils.repo_server_context(args[0]):
            return func(*args, **kwargs)
    return wrapper


def _sync_rpm_repository(repo_path, yum_config, repos):
    lock_path = os.path.join(repo_path, '.lock')

    if not os.path.exists(repo_path):
        os.makedirs(repo_path)

    with lockfile.LockFile(lock_path):
        ret, _, _ = utils.run_command(
            [
                'reposync',
                '--config=%s' % yum_config,
                '--download_path=%s' % repo_path,
                '--newest-only',
                '--delete',
            ] + [
                '--repoid=%s' % repo
                for repo in repos
            ],
        )
        if ret:
            repoverify.verify_reposync(yum_config, repo_path, repos)


def _build_rpms(name, script, source_dir, output_dir, dists, env=None):
    logging.info(
        'Building %s(%s) from %s, for %s, storing results in %s',
        name,
        script,
        source_dir,
        ', '.join(dists),
        output_dir,
    )
    ret, out, err = utils.run_command(
        [
            script,
            source_dir,
            output_dir,
        ] + dists,
        env=env,
    )

    if ret:
        logging.error(
            '%s returned with error %d',
            script,
            ret,
        )
        logging.error('Output was: \n%s', out)
        logging.error('Errors were: \n%s', err)
        raise RuntimeError('%s failed, see logs' % script)

    return ret


def _build_vdsm_rpms(vdsm_dir, output_dir, dists):
    _build_rpms('vdsm', 'build_vdsm_rpms.sh', vdsm_dir, output_dir, dists)


def _build_engine_rpms(engine_dir, output_dir, dists, build_gwt=False):
    env = os.environ.copy()
    if build_gwt:
        env['BUILD_GWT'] = '1'
    else:
        env['BUILD_GWT'] = '0'
    _build_rpms(
        'ovirt-engine',
        'build_engine_rpms.sh',
        engine_dir,
        output_dir,
        dists,
        env
    )


def _build_vdsm_jsonrpc_java_rpms(source_dir, output_dir, dists):
    _build_rpms(
        'vdsm-jsonrpc-java',
        'build_vdsm-jsonrpc-java_rpms.sh',
        source_dir,
        output_dir,
        dists
    )


def _git_revision_at(path):
    ret, out, _ = utils.run_command(
        ['git', 'rev-parse', 'HEAD'],
        cwd=path
    )
    if ret:
        return 'unknown'
    return out.strip()


def _activate_storage_domains(api, sds):
    for sd in sds:
        sd.activate()

    for sd in sds:
        dc = api.datacenters.get(
            id=sd.get_data_center().get_id(),
        )
        testlib.assert_true_within_long(
            lambda: dc.storagedomains.get(sd.name).status.state == 'active',
        )


def _deactivate_storage_domains(api, sds):
    for sd in sds:
        sd.deactivate()

    for sd in sds:
        dc = api.datacenters.get(id=sd.get_data_center().get_id())
        testlib.assert_true_within_long(
            lambda:
            dc.storagedomains.get(sd.name).status.state == 'maintenance',
        )


def _deactivate_all_storage_domains(api):
    for dc in api.datacenters.list():
        sds = dc.storagedomains.list()
        _deactivate_storage_domains(api, [sd for sd in sds if not sd.master])
        _deactivate_storage_domains(api, [sd for sd in sds if sd.master])


def _deactivate_all_hosts(api):
    hosts = api.hosts.list()

    while hosts:
        host = hosts.pop()
        try:
            host.deactivate()
            logging.info('Sent host %s to maintenance', host.name)
        except RequestError:
            logging.exception('Failed to maintenance host %s', host.name)
            hosts.insert(0, host)

    for host in api.hosts.list():
        logging.debug('Waiting for %s to go into maintenance', host.name)
        testlib.assert_true_within_short(
            lambda: api.hosts.get(host.name).status.state == 'maintenance',
        )


def _activate_all_hosts(api):
    names = [host.name for host in api.hosts.list()]

    for name in names:
        try:
            api.hosts.get(name).activate()
        except RequestError:
            pass

    for name in names:
        testlib.assert_true_within_short(
            lambda: api.hosts.get(name).status.state == 'up',
        )


def _activate_all_storage_domains(api):
    for dc in api.datacenters.list():
        sds = dc.storagedomains.list()
        _activate_storage_domains(api, [sd for sd in sds if sd.master])
        _activate_storage_domains(api, [sd for sd in sds if not sd.master])


class OvirtPrefix(testenv.Prefix):
    def _create_paths(self):
        return paths.OvirtPaths(self._prefix)

    def create_snapshots(self, name, restore=True):
        with testenv.utils.RollbackContext() as rollback:
            engine = self.virt_env.engine_vm()

            self._deactivate()
            rollback.prependDefer(self._activate)

            logging.info('Creating snapshots')
            # stop engine:
            engine.service('ovirt-engine').stop()
            rollback.prependDefer(engine.get_api)
            rollback.prependDefer(engine.service('ovirt-engine').start)

            # stop VDSMs:
            def stop_host(host):
                host.service('vdsmd').stop()
                rollback.prependDefer(host.service('vdsmd').start)

                host.service('supervdsmd').stop()
                rollback.prependDefer(host.service('supervdsmd').start)

            vec = testenv.utils.func_vector(
                stop_host,
                [(vm,) for vm in self.virt_env.host_vms()],
            )
            vt = testenv.utils.VectorThread(vec)
            vt.start_all()
            vt.join_all()

            super(OvirtPrefix, self).create_snapshots(name)

            if not restore:
                rollback.clear()

    def revert_snapshots(self, name):
        super(OvirtPrefix, self).revert_snapshots(name)
        self._activate()

    def _create_rpm_repository(self, dists, repos_path, repo_names):
        for dist in dists:
            dist_output = self.paths.internal_repo(dist)
            rpm_dirs = []

            if os.path.exists(self.paths.build_dir('vdsm')):
                rpm_dirs.append(
                    os.path.join(self.paths.build_dir('vdsm'), dist)
                )

            if os.path.exists(self.paths.build_dir('ovirt-engine')):
                rpm_dirs.append(
                    os.path.join(self.paths.build_dir('ovirt-engine'), dist)
                )
            rpm_dirs.extend(
                [
                    os.path.join(repos_path, name)
                    for name in repo_names
                    if name.endswith(dist)
                ],
            )

            merge_repos.merge(dist_output, rpm_dirs)

    def prepare_repo(
        self,
        rpm_repo=None,
        reposync_yum_config=None,
        skip_sync=False,
        vdsm_dir=None,
        engine_dir=None,
        engine_build_gwt=None,
        vdsm_jsonrpc_java_dir=None,
    ):
        # Detect distros from template metadata
        engine_dists = []
        if self.virt_env.engine_vm():
            engine_dists.append(self.virt_env.engine_vm().distro())

        vdsm_dists = []
        for host in self.virt_env.host_vms():
            if host.distro() not in vdsm_dists:
                vdsm_dists.append(host.distro())

        all_dists = list(set(engine_dists + vdsm_dists))

        repos = []
        jobs = []

        if rpm_repo and reposync_yum_config:
            parser = ConfigParser.SafeConfigParser()
            with open(reposync_yum_config) as f:
                parser.readfp(f)
            repos = [
                repo
                for repo in parser.sections()
                if repo.split('-')[-1] in all_dists
            ]

            if not skip_sync:
                jobs.append(
                    functools.partial(
                        _sync_rpm_repository,
                        rpm_repo,
                        reposync_yum_config,
                        repos,
                    )
                )

        metadata = self._get_metadata()

        if vdsm_dir and vdsm_dists:
            jobs.append(
                functools.partial(
                    _build_vdsm_rpms,
                    vdsm_dir=vdsm_dir,
                    output_dir=self.paths.build_dir('vdsm'),
                    dists=vdsm_dists,
                )
            )

        if engine_dir and engine_dists:
            jobs.append(
                functools.partial(
                    _build_engine_rpms,
                    engine_dir=engine_dir,
                    output_dir=self.paths.build_dir('ovirt-engine'),
                    dists=engine_dists,
                    build_gwt=engine_build_gwt,
                ),
            )

        if vdsm_jsonrpc_java_dir and engine_dists:
            jobs.append(
                functools.partial(
                    _build_engine_rpms,
                    engine_dir=engine_dir,
                    output_dir=self.paths.build_dir('ovirt-engine'),
                    dists=engine_dists,
                    build_gwt=engine_build_gwt,
                ),
            )

        vt = testenv.utils.VectorThread(jobs)
        vt.start_all()
        if engine_dir:
            metadata['ovirt-engine-revision'] = _git_revision_at(engine_dir)
        if vdsm_dir:
            metadata['vdsm-revision'] = _git_revision_at(vdsm_dir)
        vt.join_all()

        self._create_rpm_repository(all_dists, rpm_repo, repos)
        self.save()

    @_with_repo_server
    def run_test(self, path):
        logging.info('Running test: %s', path)
        env = os.environ.copy()
        env['TESTENV_PREFIX'] = self.paths.prefix()

        extra_args = [
            '--with-xunit',
            '--xunit-file=%s' % (
                os.path.abspath(
                    os.path.join(
                        self.paths.prefix(),
                        'nosetests-%s.xml' % os.path.basename(path),
                    )
                )
            ),
            '--with-log-collector-plugin',
        ]

        config = nose.config.Config(
            verbosity=3,
            env=env,
            plugins=nose.core.DefaultPluginManager(),
        )
        addplugins = [
            testlib.LogCollectorPlugin(self),
        ]

        return nose.core.run(
            argv=['testrunner', path] + extra_args,
            config=config,
            addplugins=addplugins,
        )

    def _deploy_host(self, host):
        host.wait_for_ssh()
        for script in host.metadata.get('ovirt-scripts', []):
            ret, _, _ = host.ssh_script(script, show_output=False)
            if ret != 0:
                raise RuntimeError(
                    '%s failed with status %d on %s' % (
                        script,
                        ret,
                        host.name(),
                    ),
                )

    @_with_repo_server
    def deploy(self):
        jobs = []
        for host in self.virt_env.get_vms().values():
            jobs.append(
                functools.partial(self._deploy_host, host=host)
            )
        vt = testenv.utils.VectorThread(jobs)
        vt.start_all()
        vt.join_all()

    def _create_virt_env(self):
        return virt.OvirtVirtEnv.from_prefix(self)

    def _activate(self):
        for vm in self.virt_env.get_vms().values():
            vm.wait_for_ssh()
        logging.info('Hosts up')
        api = self.virt_env.engine_vm().get_api()
        _activate_all_hosts(api)
        logging.info('Hosts activated')
        _activate_all_storage_domains(api)
        logging.info('Storage domains activated')

    def _deactivate(self):
        api = self.virt_env.engine_vm().get_api()

        _deactivate_all_storage_domains(api)
        _deactivate_all_hosts(api)

    def start(self):
        super(OvirtPrefix, self).start()
        self._activate()

    def stop(self):
        self._deactivate()
        super(OvirtPrefix, self).stop()

    def collect_artifacts(self, output_dir):
        os.makedirs(output_dir)

        def _collect_artifacts(vm, path):
            os.makedirs(path)
            vm.collect_artifacts(path)

        vt = testenv.utils.VectorThread(
            [
                functools.partial(
                    _collect_artifacts,
                    vm,
                    os.path.join(
                        output_dir,
                        vm.name(),
                    ),
                )
                for vm in self.virt_env.get_vms().values()
            ],
        )
        vt.start_all()
        vt.join_all()
