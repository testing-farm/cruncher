import os
import shutil
import tempfile
from exceptions import OSError

import gluetool

from gluetool.utils import cached_property, check_for_commands, log_blob, Command, PatternMap

REQUIRED_CMDS = ['copr']


class Cruncher(gluetool.Module):
    name = 'cruncher'

    options = [
        ('Copr options', {
            'copr-build-id': {
                'help': 'Copr build ID',
                'type': int
            },
            'copr-chroot': {
                'help': 'Chroot identification'
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
            }
        }),
        ('Testing options', {
            'cleanup': {
                'help': 'Cleanup all created files.',
                'action': 'store_true'
            }
        })
    ]

    required_options = ['chroot-image-map', 'copr-build-id', 'copr-chroot']

    def __init__(self, *args, **kwargs):
        super(Cruncher, self).__init__(*args, **kwargs)

        check_for_commands(REQUIRED_CMDS)

        self.workdir = None
        self.curdir = os.getcwd()

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

        except gluetool.GlueCommandError as e:

            log_blob(self.error, "Last 30 lines of '{}'".format(' '.join(command)), e.output.stderr)

            raise gluetool.GlueError('Failed to download copr build')

    def provision(self):

        script = self.option('provision-script')

        command = ['python3', script, self.image]
        environment = os.environ.copy()

        environment.update({'TEST_DEBUG': '1'})

        try:
            self.info("Booting image '{}'".format(self.image))

            Command(command).run(cwd=self.workdir, env=environment)

        except gluetool.GlueCommandError as e:

            log_blob(self.error, "Last 30 lines of '{}'".format(' '.join(command)), e.output.stderr)

            raise gluetool.GlueError('Failed to boot')
t
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
        guest = self.provision()

    def destroy(self, failure):

        # Cleanup workdir if needed
        if self.option('cleanup'):
            self.info("Removing workdir '{}'".format(self.workdir))
            shutil.rmtree(self.workdir)

