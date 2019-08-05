import colorama
import fmf
import jinja2
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from exceptions import OSError

import gluetool
from gluetool.log import ContextAdapter, LoggerMixin

# pylint: disable=line-too-long
from gluetool.utils import cached_property, check_for_commands, from_json, requests, render_template, Command, SimplePatternMap
from gluetool.log import log_blob, log_dict


def get_url_content(url):
    with requests() as req:
        response = req.get(url)

    response.raise_for_status()

    return response.content


jinja2.defaults.DEFAULT_FILTERS['get_url_content'] = get_url_content


REQUIRED_CMDS = ['copr', 'curl', 'ansible-playbook', 'git']


class Guest(LoggerMixin, object):
    def __init__(self, module, host, key, port, user='root'):
        super(Guest, self).__init__(module.logger)

        self.module = module
        self.key = key
        self.port = port
        self.host = host
        self.home = None

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
            Command(command).run()

        except gluetool.GlueCommandError as error:

            log_blob(self.error, "Last 30 lines of '{}'".format(' '.join(command)), error.output.stderr)

            raise gluetool.GlueError("Failed to connect to guest '{}' via ssh".format(self.user_host))

    def set_home(self, path):
        self.home = path

    def run(self, command, pipe=False):
        run_command = command

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

        if self.home:
            command = 'cd {}; {}'.format(self.home, command)

        print(colorama.Fore.CYAN + '# {}'.format(run_command))

        if pipe:
            process = subprocess.Popen(ssh_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        else:
            process = subprocess.Popen(ssh_command, stderr=subprocess.STDOUT)
        stdout, stderr = process.communicate()

        if process.returncode != 0:
            raise gluetool.GlueError("Command '{}' failed with return code '{}'".format(command, process.returncode))

        return stdout, stderr

    def run_playbook(self, playbook):
        self.info("[prepare] Running playbook '{}'".format(playbook))

        command = [
            'ansible-playbook',
            '-i', '{},'.format(self.user_host),
            '--ssh-common-args=\'-i{}\''.format(self.key),
            '--ssh-extra-args=\'-p{}\''.format(self.port),
            '--scp-extra-args=\'-P{}\''.format(self.port),
            '--sftp-extra-args=\'-P{}\''.format(self.port),
            playbook
        ]

        self.debug(' '.join(command))
        process = subprocess.Popen(command, stderr=subprocess.STDOUT)
        process.communicate()
        if process.returncode != 0:
            raise gluetool.GlueError("Playbook '{}' failed with return code '{}'".format(command, process.returncode))


class TestSetAdapter(ContextAdapter):
    """
    Displays testset name as a context.
    """
    def __init__(self, logger, testset, *args, **kwargs):
        super(TestSetAdapter, self).__init__(logger, {'ctx_testset': (110, testset)})


class TestSet(LoggerMixin):
    """ Testset object handling individual steps """

    # Working directories (shared and testset-specific)
    _shared_workdir = None
    _workdir = None

    def __init__(self, module, testset, cruncher, *args, **kwargs):
        """ Initialize testset steps """
        super(TestSet, self).__init__(TestSetAdapter(module.logger, testset.name), *args, **kwargs)

        if testset.get('summary'):
            cruncher.info("summary: {}".format(testset.get('summary')))

        # Initialize values
        self.cruncher = cruncher
        self.testset = testset
        self.tests = None
        self.results = []
        self.guest = None
        self.source = '~/source'

        # Dashed name of the testset without the /ci/test/ prefix
        self.name = self.testset.name.lstrip('/ci/test/').replace('/', '-')

        # Execute step should be defined
        if not self.testset.get('execute'):
            raise gluetool.GlueError('No "execute" step in the testset {}'.format(testset.name))

    def go(self):
        """ Go and process the testset step by step """
        self.discover()
        self.provision()
        self.prepare()
        self.execute()
        results = self.report()
        self.finish()

        return results

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
                self.info('Using working directory: {}'.format(workdir))
            else:
                prefix = 'workdir-{}-'.format(self.cruncher.option('copr-chroot'))
                try:
                    workdir = tempfile.mkdtemp(prefix=prefix, dir='.')
                except OSError as error:
                    raise gluetool.GlueError('Could not create working directory: {}'.format(error))
                self.info('[prepare] Working directory created: {}'.format(workdir))
            TestSet._shared_workdir = workdir

        # Create testset-specific subdirectory
        self._workdir = os.path.join(TestSet._shared_workdir, self.name)
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
            repository = str(discover.get('repository'))
            if not repository:
                raise gluetool.GlueError("No repository defined in the discover step")
            self.info("[discover] Checking repository '{}'".format(repository))
            try:
                command = ['git', 'clone', repository, 'tests']
                Command(command).run(cwd=self.workdir)
            except gluetool.GlueCommandError as error:
                log_blob(self.cruncher.error, "Tail of stderr '{}'".format(' '.join(command)), error.output.stderr)
                raise gluetool.GlueError("Failed to clone the git repository '{}'".format(repository))
            # Pick tests based on the fmf filter
            tree = fmf.Tree(os.path.join(self.workdir, 'tests'))
            filters = discover.get('filter')
            self.tests = list(tree.prune(keys=['test'], filters=[filters] if filters else []))
            log_dict(self.info, "[discover] Discovered tests", [test.name for test in self.tests])

    def provision(self):
        """ Provision the guest """

        # Use an already running instance if provided
        if self.cruncher.option('ssh-host'):
            self.info("[provision] Using provided running instance '{}'".format(
                self.cruncher.option('ssh-host')))
            self.guest = Guest(
                self.cruncher,
                self.cruncher.option('ssh-host'),
                self.cruncher.option('ssh-key'),
                self.cruncher.option('ssh-port'))
            return

        # Create a new instance using the provision script
        self.info("[provision] Booting image '{}'".format(self.cruncher.image))
        command = ['python3', self.cruncher.option('provision-script'), self.cruncher.image]
        environment = os.environ.copy()
        # FIXME: not sure if so much handy ...
        # environment.update({'TEST_DEBUG': '1'})
        try:
            output = Command(command).run(env=environment)
        except gluetool.GlueCommandError as error:
            # log_blob(self.cruncher.error, "Tail of stderr '{}'".format(' '.join(command)), error.output.stderr)
            raise gluetool.GlueError("Failed to boot test machine with image '{}', is /dev/kvm available?".format(self.cruncher.image))

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
            self.debug('No copr repository specified, skipping installation')
            return

        self.info('[prepare] Installing builds from copr')

        # Enable copr repository and install all builds from there
        self.guest.run('dnf -y copr enable {}'.format(self.cruncher.option('copr-name')))

        # Install all builds from copr repository
        self.guest.run(
            # pylint: disable=line-too-long
            'dnf -q repoquery --disablerepo=* --enablerepo=copr:$(dnf -y copr list enabled | tr "/" ":") | grep -v \\.src | xargs dnf -y install'
        )

    def download_git(self):
        """ Download git repository to the machine """
        # Nothing to do if git-url and git-ref not specified
        url = self.cruncher.option('git-url')
        ref = self.cruncher.option('git-ref')

        if not url and not ref:
            self.debug('No git repository for download specified, skipping download')
            return

        self.info("[prepare] Download git repository '{}' ref '{}' to '{}' on test machine".format(url, ref, self.source))

        self.guest.run('rpm -q git >/dev/null || dnf -y install git')
        self.guest.run('rm -rf {1} && git clone --depth 1 {0} {1}'.format(url, self.source))
        self.guest.run('cd {0} && git fetch origin {1}:{1} && git checkout ref'.format(self.source, ref))

        self.info("[prepare] Using cloned repository as working directory on the test machine")
        self.guest.set_home(self.source)

    def prepare(self):
        """ Prepare the guest for testing """
        # Install copr build
        self.install_copr_build()

        # Download git sources
        self.download_git()

        # Handle the prepare step
        prepare = self.testset.get('prepare')
        if not prepare:
            return
        if prepare.get('how') == 'ansible':
            self.info('[prepare] Applying ansible playbooks')
            self.guest.run('dnf -y install python')
            for playbook in prepare.get('playbooks'):
                self.guest.run_playbook(os.path.join(self.cruncher.fmf_root, playbook))

        # Prepare for beakerlib testing
        if self.testset.get(['execute', 'how']) == 'beakerlib':
            self.info('[prepare] Installing beakerlib and tests')
            self.guest.run('dnf -y install beakerlib git')
            self.guest.run('echo -e "#!/bin/bash\ntrue" > /usr/bin/rhts-environment.sh') # FIXME

    def execute_beakerlib_test(self, test, path):
        """ Execute a beakerlib test """
        self.info('[execute] Running test {}'.format(test.name))
        test_dir = os.path.join(path, 'tests', (test.get('path') or test.name.lstrip('/')))
        logs_dir = os.path.join(path, 'logs', test.name.lstrip('/').replace('/', '-'))
        self.guest.run('cd {}; mkdir -p {}; BEAKERLIB_DIR={} {}'.format(
            test_dir, logs_dir, logs_dir, test.get('test')))
        journal, _ = self.guest.run('cat {}'.format(os.path.join(logs_dir, 'journal.txt')), pipe=True)
        if re.search("OVERALL RESULT: PASS", journal):
            result = "pass"
        elif re.search("OVERALL RESULT: FAIL", journal):
            result = "fail"
        else:
            result = "error"

        self.results[test.name] = result

    def execute(self):
        """ Execute discovered tests """
        execute = self.testset.get('execute')
        # Shell
        if execute.get('how') == 'shell':
            self.info('[execute] Running shell commands')

            try:
                for command in execute['commands']:
                    self.guest.run(command)
                result = "pass"

            except gluetool.GlueError as error:
                self.error(error)
                result = "fail"

            self.results.append({
                'name': self.testset.name,
                'result': result,
                # we need to find out how to save the logs
                'log': 'FIXME'
            })

        # Beakerlib
        if execute.get('how') == 'beakerlib':
            # Fetch tests on the guest
            path = os.path.join('/tmp', self.name)
            self.guest.run('rm -rf {}; mkdir -p {}/logs; cd {}; git clone {} tests'.format(
                path, path, path, str(self.testset.get(['discover', 'repository']))))
            self.info('[execute] Running beakerlib tests')
            # Execute tests
            for test in self.tests:
                self.execute_beakerlib_test(test, path)

    def report(self):
        """ Report results """
        return self.results

    def finish(self):
        """ Finishing tasks """
        # Keep the vm running
        if self.cruncher.option('keep-instance') or self.cruncher.option('ssh-host'):
            self.info("[finish] Guest kept running, use "
                "'--ssh-host={} --ssh-key={} --ssh-port={}' to connect back with cruncher".format(
                self.guest.host, self.guest.key, self.guest.port))
            self.info("[finish] Use 'ssh -i {} -p {} root@{}' to connect to the test machine".format(
                self.guest.key, self.guest.port, self.guest.host))
        # Destroy the vm
        else:
            self.info('[finish] Destroying test machine')
            try:
                Command(['pkill', '-9', 'qemu']).run()
            except gluetool.GlueCommandError:
                pass


class Cruncher(gluetool.Module):
    name = 'cruncher'
    description = 'Cruncher for FMF flock prototype'

    options = [
        ('Service related options', {
            'artifact-dir': {
                'help': 'Directory where to save artifacts from testing on localhost. Requires ``pipeline-id`` options.',
            },
            'artifact-root-url': {
                'help': 'Root of the URL to the artifacts',
            },
            'ui-url': {
                'help': 'URL to the user interface.'
            }
        }),
        ('Copr artifact options', {
            'copr-chroot': {
                'help': 'Copr chroot name'
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
        ('Test options - localhost', {
            'fmf-root': {
                'help': 'Path to the fmf root tree on localhost. Useful for running localhost tests.'
            },
        }),
        ('Test options - git', {
            'git-url': {
                'help': 'URL to git repository with FMF L2 metadata.'
            },
            'git-ref': {
                'help': 'Git reference to checkout'
            },
            'git-commit-sha': {
                'help': 'Git commit SHA',
            },
            'git-repo-name': {
                'help': 'Git repository name'
            },
            'git-repo-namespace': {
                'help': 'Git repository namespace'
            }
        }),
        ('Provision options', {
            'provision-script': {
                'help': 'Provision script to use'
            },
        }),
        ('Reporting options', {
            'pipeline-id': {
                'help': 'A globally unique ID which identifies the test.',
            },
            'post-results': {
                'help': 'Post results to URL defined in ``post-results-url``',
                'action': 'store_true'
            },
           'post-results-url': {
                'help': 'URL(s) for posting results.',
                'action': 'append'
           }
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
                'help': 'Use given workdir instead of current directory',
                'default': '.'
            }
        })
    ]

    def __init__(self, *args, **kwargs):
        super(Cruncher, self).__init__(*args, **kwargs)

        check_for_commands(REQUIRED_CMDS)

        self.guest = None
        self.image = None

        colorama.init(autoreset=True)

        self.results = []

    def sanity(self):

        self.image = self.option('image-file')

        # copr-chroot and copr-name are required
        if (self.option('copr-chroot') and not self.option('copr-name')) or \
                (self.option('copr-name') and not self.option('copr-chroot')):
            raise gluetool.utils.IncompatibleOptionsError(
               'Insufficient information about copr build supplied, both chroot and repository name need to be specified.')

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

        # resolve image from URL
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
        git_url = self.option('git-url')
        git_ref = self.option('git-ref')
        workdir = self.option('workdir')

        if self.fmf_root:
            self.info("Getting FMF metadata from local path '{}' ".format(self.fmf_root))

        elif git_url and git_ref:
            self.info("Getting FMF metadata from git repository '{}' ref '{}' ".format(git_url, git_ref))
            Command(['rm', '-rf', 'source']).run(cwd=workdir)
            Command(['git', 'clone', '--depth=1', git_url, 'source' ]).run(cwd=workdir)
            Command(['git', 'fetch', 'origin', '{0}:{0}'.format(git_ref)]).run(cwd=os.path.join(workdir, 'source'))
            Command(['git', 'checkout', git_ref]).run(cwd=os.path.join(workdir, 'source'))
            self.fmf_root = os.path.join(workdir, 'source')

        else:
            self.info("Nothing to do, no FMF metadata provided")

        # init FMF trees
        try:
            tree = fmf.Tree(self.fmf_root)
        except fmf.utils.RootError:
            raise gluetool.GlueError('No FMF metadata found.')

        # FIXME: blow up if no tests to run
        # FIXME: other checks for fmf validity?

        # Process each testset found in the fmf tree
        for testset in tree.climb():
            testset = TestSet(self, testset, cruncher=self)
            self.results.extend(testset.go())

        if self.results:
            log_dict(self.info, "Test results", self.results)

    def post_results(self, failure):
        if not self.option('post-results'):
            return

        message = {}

        # if there was a failure or no results, we report error
        if failure:
            result = "error"
            message['message'] = str(failure.exc_info[1])

        elif not self.results:
            result = "error"
            message['message'] = "No tests were defined in FMF metadata."

        else:
            result = 'pass'

            if any([r['result'] == 'fail' for r in self.results]):
                result = 'fail'

        message['result'] = result

        pipeline_id = self.option('pipeline-id')

        if pipeline_id:
            message.update({'pipeline': pipeline_id})

        if self.option('copr-name'):
            message.update({
                'artifact': {
                    'repo-name': self.option('git-repo-name'),
                    'repo-namespace': self.option('git-repo-namespace'),
                    'copr-repo-name': self.option('copr-name'),
                    'copr-chroot': self.option('copr-chroot'),
                    'commit-sha': self.option('git-commit-sha'),
                    'git-url': self.option('git-url'),
                    'git-ref': self.option('git-ref')
                }
            })

        if self.image:
            message.update({
                'environment': {
                    'image': os.path.basename(self.image)
                }
            })

        if self.results:
            message.update({
                'tests': self.results,
            })            

        message.update({
            'url': '{}/pipeline/{}'.format(self.option('ui-url'), pipeline_id),
        })

        log_dict(self.info, 'result message', message)

        log_dict(self.info, 'posting result message to URLs', self.option('post-results-url'))

        for url in self.option('post-results-url'):
            with requests(logger=self.logger) as req:
                response = req.post(url, json=message)

            if response.status_code != 200:
                raise gluetool.GlueError('Could not post results to "{}"'.format(url))

    def destroy(self, failure):
        """ Cleanup and notification tasks """
        # post results about testing
        self.post_results(failure)

        # Remove workdir if requested
        if self.option('cleanup') and TestSet._workdir:
            self.info("Removing workdir '{}'".format(TestSet._workdir))
            shutil.rmtree(TestSet._workdir)
