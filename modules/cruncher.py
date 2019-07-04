import os
import shutil
import tempfile
from exceptions import OSError

import gluetool

from gluetool.utils import cached_property, check_for_commands, from_json, log_blob, log_dict, Command, PatternMap

REQUIRED_CMDS = ['copr']


class Guest(object):
    def __init__(self, module, ip, key=None, control=None, pid=None, user='root', port=22):
        self.module = module
        self.control = control or '{}/{}@{}:{}'.format(module.workdir, user, ip, port)
        self.pid = pid

        module.logger.connect(self)

        ssh_user_host = '{}@{}'.format(user, ip)

        if control:

            self.info('reusing SSH connection to the VM')

            command = [
                'ssh', '-S', control, ssh_user_host, 'echo'
            ]

        else:

            self.info('initializing new SSH connection to the VM')

            command = [
                'ssh', '-fMN', '-S',
                self.control,
                '-o', 'StrictHostKeyChecking=no',
                '-i', key,
                '-p', port,
                ssh_user_host,
                'echo'
            ]

        try:
            output = Command(command).run()

        except gluetool.GlueCommandError as error:
        
            log_blob(self.error, "Last 30 lines of '{}'".format(' '.join(command)), error.output.stderr)

            raise gluetool.GlueError("Failed to connect to guest '{}' via ssh".format(self.ssh_user_host))

    def run(self, command):
        try:
            Command(['ssh', '-S', self.control, self.ssh_user_host, command]).run()
        except gluetool.GlueCommandError as error:
            log_blob(self.error, 'stdout', error.output.stderr)
            log_blob(self.error, 'stderr', error.output.stderr)

            raise gluetool.GlueError("Failed to run command '{}'".format(command))

    # def upload(self, from_path, to_path):

class Cruncher(gluetool.Module):
    name = 'cruncher'
    description = 'Cruncher for FMF flock prototype'

    options = [
        ('Copr options', {
            'copr-build-id': {
                'help': 'Copr build ID',
                'type': int
            },
            'copr-chroot': {
                'help': 'Chroot identification'
            },
            'copr-name': {
                'help': 'Copr repository name'
            }
        }),
        ('Image options', {
            'chroot-image-map': {
                'help': 'Chroot to image mapping file'
            }
        }),
        ('Provision options', {
            'provision-script': {
                'help': 'Provision script to use'
            },
        }),
        ('Testing options', {
            'ssh-control': {
                'help': 'SSH control socket to use for connecting to an existing host (disables provisioning)',
            },
            'ssh-host': {
                'help': 'SSH existing host name (disables provisioning)',
            },
            'cleanup': {
                'help': 'Cleanup all created files, including control files, etc.',
                'action': 'store_true'
            },
            'keep-instance': {
                'help': 'Keep instance running, use ``ssh-control`` to connect back to te instance.',
                'action': 'store_true'
            }
        })
    ]

    required_options = ['chroot-image-map', 'copr-build-id', 'copr-chroot', 'copr-name']

    def __init__(self, *args, **kwargs):
        super(Cruncher, self).__init__(*args, **kwargs)

        check_for_commands(REQUIRED_CMDS)

        self.workdir = None
        self.guest = None

    @cached_property
    def image(self):
        """
        Maps copr chroot to a specific image.
        """
        return PatternMap(self.option('chroot-image-map'), logger=self.logger).match(self.option('copr-chroot'))

    def download_copr_build(self):
        """
        Downloads copr directory to the given workdir.
        """
        build_id = str(self.option('copr-build-id'))
        chroot = self.option('copr-chroot')

        command = ['copr', 'download-build', '--chroot', chroot, build_id]

        try:
            self.info("Downloading copr build id '{}' chroot '{}'".format(build_id, chroot))

            Command(command).run(cwd=self.workdir)

        except gluetool.GlueCommandError as error:

            log_blob(self.error, "Last 30 lines stderr of '{}'".format(' '.join(command)), error.output.stderr)

            raise gluetool.GlueError('Failed to download copr build')

    def provision(self):

        # init an existing ...
        if self.option('ssh-control'):
            return Guest(self, self.option('ssh-host'), control=self.option('ssh-control'))

        script = self.option('provision-script')

        command = ['python3', script, self.image]
        environment = os.environ.copy()

        environment.update({'TEST_DEBUG': '1'})

        try:
            self.info("Booting image '{}'".format(self.image))

            output = Command(command).run(cwd=self.workdir, env=environment)

        except gluetool.GlueCommandError as error:

            log_blob(self.error, "Last 30 lines of stderr '{}'".format(' '.join(command)), error.output.stderr)

            raise gluetool.GlueError("Failed to boot image '{}'".format(self.image))

        details = from_json(output.stdout)

        log_dict(self.debug, 'provisioning details', details)

        host = details['localhost']['hosts'][0]
        host_details = details['_meta']['hostvars'][host]
        guest = Guest(self, host_details['ansible_host'],
                            key=host_details['ansible_ssh_private_key_file'],
                            pid=host_details['pid'],
                            port=host_details['ansible_port'],
                            user=host_details['ansible_user'])

        return guest

    def execute(self):

        # Create working directory in the current dir
        prefix = 'build-{}-{}'.format(str(self.option('copr-build-id')), self.option('copr-chroot'))

        try:
            self.workdir = tempfile.mkdtemp(prefix=prefix, dir='.')

        except OSError as e:
            raise gluetool.GlueError('Could not create working directory: {}'.format(e))

        self.info('Working directory: {}'.format(self.workdir))

        self.info('Using image: {}'.format(self.image))

        # Download copr build
        self.download_copr_build()

        # Provision qcow2
        self.guest = self.provision()

        self.guest.run('echo hello bajrou!')

    def destroy(self, failure):

        if self.option('keep-instance'):
            self.info("Guest kept running, control file '{}'".format(self.guest.control))

        else:
            # Try to remove VM
            try:
                Command(['pkill', '-9', 'qemu']).run()
            except gluetool.GlueCommandError:
                pass

        # Cleanup workdir if needed
        if self.option('cleanup'):
            self.info("Removing workdir '{}'".format(self.workdir))
            shutil.rmtree(self.workdir)

