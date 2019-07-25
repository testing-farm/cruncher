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


REQUIRED_CMDS = ['copr', 'curl', 'ansible-playbook', 'git']


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
        self.info("Prepare: Running playbook '{}'".format(playbook))

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


class TestSet(object):
    """ Testset object handling individual steps """

    # Working directories (shared and testset-specific)
    _shared_workdir = None
    _workdir = None

    def __init__(self, testset, cruncher):
        """ Initialize testset steps """
        if testset.get('summary'):
            cruncher.info("Testset: {} ({})".format(testset.get('summary'), testset.name))
        else:
            cruncher.info("Testset: {}".format(testset.name))

        # Initialize values
        self.cruncher = cruncher
        self.testset = testset
        self.tests = None
        self.guest = None

        # Execute step should be defined
        if not self.testset.get('execute'):
            raise gluetool.GlueError('No "execute" step in the testset {}'.format(testset.name))

    def go(self):
        """ Go and process the testset step by step """
        self.discover()
        self.provision()
        self.prepare()
        self.execute()
        self.report()
        self.finish()

    @property
    def workdir(self):
        """ Testset working directory under the shared workdir """

        # Nothing to do if directory already created
        if self._workdir is not None:
            return self._workdir

        # Use provided shared workdir or create one in the current directory
        if TestSet._shared_workdir is None:
            workdir = self.cruncher.option('workdir')
            if workdir:
                self.cruncher.info('Using working directory: {}'.format(workdir))
            else:
                prefix = 'workdir-{}-'.format(self.cruncher.option('copr-chroot'))
                try:
                    workdir = tempfile.mkdtemp(prefix=prefix, dir='.')
                except OSError as error:
                    raise gluetool.GlueError('Could not create working directory: {}'.format(error))
                self.cruncher.info('Working directory created: {}'.format(workdir))
            TestSet._shared_workdir = workdir

        # Create testset-specific subdirectory
        self._workdir = os.path.join(
            TestSet._shared_workdir, self.testset.name.replace('/', '-').lstrip('-'))
        try:
            os.mkdir(self._workdir)
        except OSError, error:
            raise gluetool.GlueError('Could not create working directory: {}'.format(error))
        return self._workdir

    def discover(self):
        """ Discover tests for execution """
        discover = self.testset.get('discover')
        if not discover:
            return
        # FMF
        if discover.get('how') == 'fmf':
            # Clone the git repository
            repository = discover.get('repository')
            if not repository:
                raise gluetool.GlueError("No repository defined in the discover step")
            self.cruncher.info("Discover: Checking repository '{}'".format(repository))
            try:
                command = ['git', 'clone', repository, 'tests']
                output = Command(command).run(cwd=self.workdir)
            except gluetool.GlueCommandError as error:
                log_blob(self.cruncher.error, "Tail of stderr '{}'".format(' '.join(command)), error.output.stderr)
                raise gluetool.GlueError("Failed to clone the git repository '{}'".format(repository))
            # Pick tests based on the fmf filter
            tree = fmf.Tree(os.path.join(self.workdir, 'tests'))
            filters = discover.get('filter')
            self.tests = list(tree.prune(keys=['test'], filters=[filters] if filters else []))
            log_dict(self.cruncher.info, "Discovered tests", [test.name for test in self.tests])

    def provision(self):
        """ Provision the guest """

        # Use an already running instance if provided
        if self.cruncher.option('ssh-host'):
            self.cruncher.info("Provision: Using provided running instance '{}'".format(
                self.cruncher.option('ssh-host')))
            self.guest = Guest(
                self.cruncher,
                self.cruncher.option('ssh-host'),
                self.cruncher.option('ssh-key'),
                self.cruncher.option('ssh-port'))
            return

        # Create a new instance using the provision script
        self.cruncher.info("Provision: Booting image '{}'".format(self.cruncher.image))
        command = ['python3', self.cruncher.option('provision-script'), self.cruncher.image]
        environment = os.environ.copy()
        environment.update({'TEST_DEBUG': '1'})
        try:
            output = Command(command).run(env=environment)
        except gluetool.GlueCommandError as error:
            log_blob(self.cruncher.error, "Tail of stderr '{}'".format(' '.join(command)), error.output.stderr)
            raise gluetool.GlueError("Failed to boot image '{}'".format(self.cruncher.image))

        # Store provision details and create the guest
        details = from_json(output.stdout)
        log_dict(self.cruncher.debug, 'provisioning details', details)
        host = details['localhost']['hosts'][0]
        host_details = details['_meta']['hostvars'][host]
        self.guest = Guest(
            self.cruncher,
            host_details['ansible_host'],
            key=host_details['ansible_ssh_private_key_file'],
            port=host_details['ansible_port'])

    def install_copr_build(self):
        """ Install packages from copr """
        # Nothing to do if copr-name not provided
        if not self.cruncher.option('copr-name'):
            return

        # Enable copr repository and install all builds from there
        self.guest.run('dnf -y copr enable {}'.format(self.cruncher.option('copr-name')))
        self.guest.run(
            'dnf -q repoquery --disablerepo=* --enablerepo={} | grep -v \.src |'
            'xargs dnf -y install'.format(self.cruncher.option('copr-name').replace('/', '-')))

    def prepare(self):
        """ Prepare the guest for testing """
        # Install copr build
        self.cruncher.info('Prepare: Installing builds from copr')
        self.install_copr_build()

        # Handle the prepare step
        prepare = self.testset.get('prepare')
        if not prepare:
            return
        if prepare.get('how') == 'ansible':
            self.cruncher.info('Prepare: Applying ansible playbooks')
            self.guest.run('dnf -y install python')
            for playbook in prepare.get('playbooks'):
                self.guest.run_playbook(os.path.join(self.cruncher.fmf_root, playbook))

    def execute(self):
        """ Execute discovered tests """
        execute = self.testset.get('execute')
        # Shell
        if execute.get('how') == 'shell':
            self.cruncher.info('Execute: Running shell commands')
            for command in execute['commands']:
                self.guest.run(command)
        # Beakerlib
        if execute.get('how') == 'beakerlib':
            self.cruncher.info('Execute: Running beakerlib tests')
            self.guest.run('dnf -y install beakerlib')

    def report(self):
        """ Report results """

    def finish(self):
        """ Finishing tasks """
        # Keep the vm running
        if self.cruncher.option('keep-instance') or self.cruncher.option('ssh-host'):
            self.cruncher.info("Finish: Guest kept running, use "
                "'--ssh-host={} --ssh-key={} --ssh-port={}' to connect back with cruncher".format(
                self.guest.host, self.guest.key, self.guest.port))
            self.cruncher.info("Finish: Use 'ssh -i {} -p {} root@{}' to connect to the VM".format(
                self.guest.key, self.guest.port, self.guest.host))
        # Destroy the vm
        else:
            self.cruncher.info('Finish: Destroying VM instance')
            try:
                Command(['pkill', '-9', 'qemu']).run()
            except gluetool.GlueCommandError:
                pass


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
            image_url = render_template(SimplePatternMap(
                self.option('image-copr-chroot-map'), logger=self.logger).match(self.option('copr-chroot')))

        if image_url:
            self.image = self.image_from_url(image_url)
            return

        if not image_url and not self.option('ssh-host'):
            raise gluetool.utils.IncompatibleOptionsError(
                'No image, SSH host or copr repository specified. Cannot continue.')

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


    def execute(self):
        """ Process all testsets defined """
        # Initialize the metadata tree
        self.fmf_root = self.option('fmf-root')
        if not self.fmf_root:
            self.info("Nothing to do, no fmf root provided")
            return
        tree = fmf.Tree(self.fmf_root)

        # Process each testset found in the fmf tree
        for testset in tree.climb():
            testset = TestSet(testset, cruncher=self)
            testset.go()

    def destroy(self, failure):
        """ Cleanup tasks """
        # Remove workdir if requested
        if self.option('cleanup') and TestSet._workdir:
            self.info("Removing workdir '{}'".format(TestSet._workdir))
            shutil.rmtree(TestSet._workdir)
