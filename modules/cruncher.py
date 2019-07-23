import colorama
import fmf
import jinja2
import os
import shutil
import subprocess
import sys
import tempfile
from exceptions import OSError

import gluetool

# pylint: disable=line-too-long
from gluetool.utils import cached_property, check_for_commands, from_json, log_blob, log_dict, requests, render_template, Command, SimplePatternMap


def get_url_content(url):
    with requests() as req:
        response = req.get(url)

    response.raise_for_status()

    return response.content


jinja2.defaults.DEFAULT_FILTERS['get_url_content'] = get_url_content


REQUIRED_CMDS = ['copr', 'curl', 'ansible-playbook']


class Guest(object):
    def __init__(self, module, host, key, port, user='root'):
        self.module = module
        self.key = key
        self.port = port
        self.host = host

        module.logger.connect(self)

        self.user_host = '{}@{}'.format(user, host)

        command = [
            'ssh',
            '-o', 'StrictHostKeyChecking=no',
            '-i', key,
            '-p', port,
            self.user_host,
            'exit'
        ]

        try:
            output = Command(command).run()

        except gluetool.GlueCommandError as error:
        
            log_blob(self.error, "Last 30 lines of '{}'".format(' '.join(command)), error.output.stderr)

            raise gluetool.GlueError("Failed to connect to guest '{}' via ssh".format(self.user_host))

    def run(self, command):
        ssh_command = [
            'ssh',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'LogLevel=ERROR',
            '-i', self.key,
            '-p', self.port,
            self.user_host,
            command
        ]

        print(colorama.Fore.CYAN + '# {}'.format(command))

        process = subprocess.Popen(ssh_command, stderr=subprocess.STDOUT)

        stdout, stderr = process.communicate()

        if process.returncode != 0:
            raise gluetool.GlueError("Command '{}' failed with return code '{}'".format(command, process.returncode))

    def run_playbook(self, playbook):
        self.info("Running playbook '{}'".format(playbook))

        command = [
            'ansible-playbook',
            '-i', '{},'.format(self.user_host),
            '--ssh-common-args=\'-i{}\''.format(self.key),
            '--ssh-extra-args=\'-p{}\''.format(self.port),
            '--scp-extra-args=\'-P{}\''.format(self.port),
            '--sftp-extra-args=\'-P{}\''.format(self.port),
            playbook
        ]

        self.info(' '.join(command))

        process = subprocess.Popen(command, stderr=subprocess.STDOUT)

        stdout, stderr = process.communicate()

        if process.returncode != 0:
            raise gluetool.GlueError("Playbook '{}' failed with return code '{}'".format(command, process.returncode))


class Cruncher(gluetool.Module):
    name = 'cruncher'
    description = 'Cruncher for FMF flock prototype'

    options = [
        ('Copr artifact options', {
            'copr-chroot': {
                'help': 'Chroot identification'
            },
            'copr-name': {
                'help': 'Copr repository name'
            }
        }),
        ('Image specification options', {
            'image-copr-chroot-map': {
                'help': 'Use image according to copr chroot specified by the given mapping file'
            },
            'image-url': {
                'help': 'Use image from given URL'
            },
            'image-file': {
                'help': 'Use image from give file'
            },
        }),
        ('Image download options', {
            'image-cache-dir': {
                'help': 'Image download path'
            },
            'no-progress': {
                'help': 'Do not show image download progress',
                'action': 'store_true'
            }
        }),
        ('Test options', {
            'fmf-root': {
                'help': 'Path to the fmf root tree'
            }
        }),
        ('Provision options', {
            'provision-script': {
                'help': 'Provision script to use'
            },
        }),
        ('Debugging options', {
            'ssh-key': {
                'help': 'SSH key to use',
            },
            'ssh-host': {
                'help': 'SSH existing host',
            },
            'ssh-port': {
                'help': 'SSH port of the existing host'
            },
            'cleanup': {
                'help': 'Cleanup all created files, including control files, etc.',
                'action': 'store_true'
            },
            'keep-instance': {
                'help': 'Keep instance running, use ``ssh-host`` to connect back to te instance.',
                'action': 'store_true'
            },
            'workdir': {
                'help': 'Use given workdir and skip copr build download'
            }
        })
    ]

    def __init__(self, *args, **kwargs):
        super(Cruncher, self).__init__(*args, **kwargs)

        check_for_commands(REQUIRED_CMDS)

        self.workdir = None
        self.guest = None
        self.image = None

        colorama.init(autoreset=True)

    def sanity(self):

        self.image = self.option('image-file')

        # Use image from path
        if self.image:
            self.info("Using image from file '{}'".format(self.image))
            return

        # Use image from url
        image_url = self.option('image-url')

        # Map image from copr repository
        if self.option('image-copr-chroot-map') and self.option('copr-chroot'):
            image_url = render_template(SimplePatternMap(self.option('image-copr-chroot-map'), logger=self.logger).match(self.option('copr-chroot')))

        if image_url:
            self.image = self.image_from_url(image_url)
            return

        if not image_url and not self.option('ssh-host'):
            raise gluetool.utils.IncompatibleOptionsError('No image, SSH host or copr repository specified. Cannot continue.')

    def image_from_url(self, url):
        """
        Maps copr chroot to a specific image.
        """
        cache_dir = self.option('image-cache-dir')
        image_name = os.path.basename(url)
        download_path = os.path.join(cache_dir, image_name)

        # check if image already exits
        if os.path.exists(download_path):
            return download_path

        self.info("Downloading image '{}' to '{}'".format(url, download_path))

        command = ['curl']

        if self.option('no-progress'):
            command.append('-s')

        command.extend([
            '-fkLo', download_path,
            url
        ])

        try:
            Command(command).run(inspect=True)

        except gluetool.GlueCommandError as error:
            # make sure that we remove the image file
            os.unlink(download_path)

            raise gluetool.GlueError('Could not download image: {}'.format(error))

        return download_path

    def provision(self, image):

        # init an existing ...
        if self.option('ssh-host'):
            return Guest(self, self.option('ssh-host'), self.option('ssh-key'), self.option('ssh-port'))

        script = self.option('provision-script')

        command = ['python3', script, image]
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
                            port=host_details['ansible_port'])

        return guest

    def create_workdir(self):
        self.workdir = self.option('workdir')

        if not self.workdir:
            # Create working directory in the current dir
            prefix = 'build-{}-{}'.format(str(self.option('copr-build-id')), self.option('copr-chroot'))

            try:
                self.workdir = tempfile.mkdtemp(prefix=prefix, dir='.')

            except OSError as e:
                raise gluetool.GlueError('Could not create working directory: {}'.format(e))

        self.info('Working directory: {}'.format(self.workdir))

    def process_fmf(self):
        fmf_root = self.option('fmf-root')

        if not fmf_root:
            return

        tree = fmf.Tree(fmf_root)

        for testset in tree.climb():
            self.info("Processing test set: '{}'".format(testset.name))

            prepare = testset.get('prepare')

            if prepare and prepare.get('how') == 'ansible':
                self.guest.run('dnf -y install python')

                for playbook in prepare.get('playbooks'):
                    self.guest.run_playbook(os.path.join(fmf_root, playbook))

            execute = testset.get('execute')

            if execute:
                for command in execute['commands']:
                    self.guest.run(command)

    def install_copr_build(self):
        if not self.option('copr-name'):
            return

        self.info('Installing builds from copr')

        # Enable copr repository
        self.guest.run('dnf -y copr enable {}'.format(self.option('copr-name')))

        # Install all build from copr repository
        # pylint: disable=line-too-lon
        self.guest.run('dnf -q repoquery --disablerepo=* --enablerepo={} | grep -v \.src | xargs dnf -y install'.format(self.option('copr-name').replace('/', '-')))

    def execute(self):

        # Provision qcow2
        self.guest = self.provision(self.image)

        # Install copr build
        self.install_copr_build()

        # Run tests
        self.process_fmf()

    def destroy(self, failure):

        if self.option('keep-instance') or self.option('ssh-host'):
            self.info("Guest kept running, use '--ssh-host={} --ssh-key={} --ssh-port={}' to connect back with cruncher".format(
                self.guest.host, self.guest.key, self.guest.port))
            self.info("Use 'ssh -i {} -p {} root@{}' to connect to the VM".format(self.guest.key, self.guest.port, self.guest.host))

        else:
            self.info('Destroying VM instance')
            # Try to remove VM
            try:
                Command(['pkill', '-9', 'qemu']).run()
            except gluetool.GlueCommandError:
                pass

        # Cleanup workdir if needed
        if self.option('cleanup'):
            self.info("Removing workdir '{}'".format(self.workdir))
            shutil.rmtree(self.workdir)

