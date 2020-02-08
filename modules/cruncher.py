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
    def __init__(self, module, host, key, port, workdir, user='root', logger=None):
        super(Guest, self).__init__(logger or module.logger)

        self.module = module
        self.key = key
        self.port = port
        self.host = host
        self.home = None
        self.workdir = workdir

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
            Command(command).run(cwd=self.workdir)

        except gluetool.GlueCommandError as error:

            log_blob(self.error, "Last 30 lines of '{}'".format(' '.join(command)), error.output.stderr)

            raise gluetool.GlueError("Failed to connect to guest '{}' via ssh".format(self.user_host))

    def set_home(self, path):
        self.home = path

    def copy(self, local_dir, remote_dir, log):
        command = [
            'scp',
            '-r',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'LogLevel=ERROR',
            '-i', self.key,
            '-P{}'.format(self.port),
            local_dir,
            '{}:{}'.format(self.user_host, remote_dir),
        ]

        log_path = os.path.join(self.workdir, log)
        with open(log_path, mode="a+") as log:

            log.write('# {}\n'.format(command))

            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            while process.poll() is None:
                line = process.stdout.readline()
                log.write(line)
                log.flush()

        if process.returncode != 0:
            raise gluetool.GlueError("Failed to archive artifacts, scp returned code '{}', see '{}' for details".format(process.returncode, log_path))

    def archive(self, from_remote_dir, to_dir, log):
        command = [
            'scp',
            '-r',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'LogLevel=ERROR',
            '-i', self.key,
            '-P{}'.format(self.port),
            '{}:{}'.format(self.user_host, from_remote_dir),
            to_dir
        ]

        log_path = os.path.join(self.workdir, log)
        with open(log_path, mode="a+") as log:

            log.write('# {}\n'.format(command))

            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            while process.poll() is None:
                line = process.stdout.readline()
                log.write(line)
                log.flush()

        if process.returncode != 0:
            raise gluetool.GlueError("Failed to archive artifacts, scp returned code '{}', see '{}' for details".format(process.returncode, log_path))


    def run(self, command, pipe=False, log=None):
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

        if log:
            log = open(os.path.join(self.workdir, log), mode="a+")

        print(colorama.Fore.CYAN + '# {}'.format(run_command))

        if log:
            log.write('# {}\n'.format(run_command))

        stdout = str()

        process = subprocess.Popen(ssh_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        while process.poll() is None:
            line = process.stdout.readline()
            if log:
                log.write(line)
                log.flush()
            sys.stdout.write(line)
            stdout += line

        if log:
            log.close()

        if process.returncode != 0:
            raise gluetool.GlueError("Command '{}' failed with return code '{}'".format(command, process.returncode))

        return stdout

    def run_playbook(self, playbook, log=None):
        self.info("[prepare] Running playbook '{}'".format(playbook))

        command = [
            'ansible-playbook',
            '-i', '{},'.format(self.user_host),
            '--ssh-common-args=\'-i{}\''.format(self.key),
            '--ssh-extra-args=\'-p{}\''.format(self.port),
            '--scp-extra-args=\'-P{}\''.format(self.port),
            '--sftp-extra-args=\'-P{}\''.format(self.port),
            '--extra-vars=ansible_python_interpreter=python3',
            playbook
        ]

        if log:
            log = open(os.path.join(self.workdir, log), mode="a+")
            log.write('# ansible-playbook -i test_machine {}\n'.format(playbook))

        self.debug(' '.join(command))

        print(colorama.Fore.CYAN + '# ansible-playbook -i test_machine {}'.format(playbook))

        stdout = str()

        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        while process.poll() is None:
            line = process.stdout.readline()
            if log:
                log.write(line)
                log.flush()
            sys.stdout.write(line)
            stdout += line

        if log:
            log.close()

        if process.returncode != 0:
            raise gluetool.GlueError("Playbook '{}' failed with return code '{}'".format(playbook, process.returncode))


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
            self.info("Summary '{}'".format(testset.get('summary')))

        # Initialize values
        self.cruncher = cruncher
        self.testset = testset
        self.tests = None
        self.results = []
        self.guest = None
        self.source = '~/git-source'
        self.remote_workdir = '~/workdir'

        # Dashed name of the testset, ignore first dash ...
        self.name = self.testset.name.replace('/', '-')[1:]

        # Workdir for the test set, based on main workdir
        self.workdir = os.path.join(self.cruncher.artifacts_dir, self.name)
        try:
            os.makedirs(self.workdir)
        except OSError as e:
            # Ignore artifact dir already exists
            if e.errno not in [17]:
                raise gluetool.GlueError("Could not create working directory '{}': {} ".format(self.workdir, str(e)))

        self.info("Testset working directory '{}'".format(self.workdir))

        # Execute step should be defined
        if not self.testset.get('execute'):
            raise gluetool.GlueError('No "execute" step in the testset {}'.format(testset.name))

    def relevant(self):
        """ Check whether given test set is relevant """
        # By default testset is relevant for all artifacts
        artifacts = self.testset.get('artifact')
        if artifacts is None:
            return True
        # Check if 'pull-request' is included in the list
        if not isinstance(artifacts, list):
            artifacts = [artifacts]
        return 'pull-request' in artifacts

    def go(self):
        """ Go and process the testset step by step """
        self.discover()
        self.provision()
        self.prepare()
        self.execute()
        results = self.report()
        self.finish()

        return results

    def discover(self):
        """ Discover tests for execution """
        discover = self.testset.get('discover')
        if not discover:
            return

        # default discover path is the current directory
        discover_path = 'source'

        # discover via FMF
        if discover.get('how') == 'fmf':

            path = self.cruncher.fmf_root

            # discover from remote repository
            if discover.get('repository'):
                discover_path = 'beakerlib-tests'

                # Clone the git repository
                repository = str(discover.get('repository'))

                self.info("[discover] Checking repository '{}'".format(repository))

                try:
                    command = ['git', 'clone', repository, discover_path]
                    Command(command).run(cwd=self.workdir)

                except gluetool.GlueCommandError as error:
                    log_blob(self.cruncher.error, "Tail of stderr '{}'".format(' '.join(command)), error.output.stderr)
                    raise gluetool.GlueError("Failed to clone the git repository '{}'".format(repository))

                path = os.path.join(self.workdir, discover_path)

            tree = fmf.Tree(path)
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
                self.cruncher.option('ssh-port'),
                self.workdir,
                logger=self.logger)

            # make sure remote workdir exits
            self.guest.run('mkdir -p {}'.format(self.remote_workdir))

            return

        # Create a new instance using the provision script
        self.info("[provision] Booting image '{}'".format(self.cruncher.image))
        command = ['python3', gluetool.utils.normalize_path(self.cruncher.option('provision-script')), self.cruncher.image]
        environment = os.environ.copy()
        # FIXME: not sure if so much handy ...
        environment.update({'KEEP_INSTANCE': '1'})
        try:
            output = Command(command).run(env=environment, cwd=self.workdir)
        except gluetool.GlueCommandError:
            # log_blob(self.cruncher.error, "Tail of stderr '{}'".format(' '.join(command)), error.output.stderr)
            # pylint: disable=line-too-long
            raise gluetool.GlueError("Failed to boot test machine with image '{}', is /dev/kvm available? See 'provision' logs for more details.".format(self.cruncher.image))

        # Store provision details and create the guest
        details = from_json(output.stdout)
        log_dict(self.cruncher.debug, 'provisioning details', details)
        host = details['localhost']['hosts'][0]
        host_details = details['_meta']['hostvars'][host]
        self.guest = Guest(
            self.cruncher,
            host_details['ansible_host'],
            host_details['ansible_ssh_private_key_file'],
            host_details['ansible_port'],
            self.workdir,
            logger=self.logger)

        # make sure remote workdir exits
        self.guest.run('mkdir -p {}'.format(self.remote_workdir))

    def install_copr_build(self):
        """ Install packages from copr """
        # Nothing to do if copr-name not provided
        if not self.cruncher.option('copr-name'):
            self.debug('No copr repository specified, skipping installation')
            return

        self.info('[prepare] Installing builds from copr')

        chroot = self.cruncher.option('copr-chroot')
        project = os.path.dirname(self.cruncher.option('copr-name'))
        repo = os.path.basename(self.cruncher.option('copr-name'))

        # Enable copr repository and install all builds from there
        if chroot.startswith('epel-6'):
            self.guest.run('cd /etc/yum.repos.d && curl -LO https://copr.fedorainfracloud.org/coprs/{}/repo/epel-6/{}-epel-6.repo'.format(
                self.cruncher.option('copr-name'),
                repo
            ))
        else:
            self.guest.run('dnf -y copr enable {}'.format(self.cruncher.option('copr-name')))

        # Install all builds from copr repository
        try:
            if chroot.startswith('epel-6') or chroot.startswith('epel-7'):
                # pylint: disable=line-too-long
                command = "repoquery --disablerepo=* --enablerepo=copr:copr.fedorainfracloud.org:{}:{} '*' | grep -v \\.src | xargs dnf -y install".format(project, repo)
            else:
                # pylint: disable=line-too-long
                command = 'dnf -q repoquery --latest 1 --disablerepo=* --enablerepo=copr:$(dnf -y copr list enabled | tr "/" ":") | grep -v \\.src | xargs dnf -y install --allowerasing'

            self.guest.run(command)
        except gluetool.GlueError:
            raise gluetool.GlueError("Error installing copr build, see console output for details.")

    def download_fmf(self):
        """ Download git repository to the machine """
        # Nothing to do if git-url and git-ref not specified
        url = self.cruncher.option('git-url')
        ref = self.cruncher.option('git-ref')
        local = self.cruncher.option('fmf-root') and gluetool.utils.normalize_path(self.cruncher.option('fmf-root'))

        if local:
            self.guest.run('rm -rf {}'.format(self.source))
            self.guest.copy(local, self.source, log='prepare.log')

        if not url or not ref:
            self.debug('No git repository for download specified, skipping download')
            return

        self.info("[prepare] Download git repository '{}' ref '{}' to '{}' on test machine".format(url, ref, self.source))

        self.guest.run('rpm -q git >/dev/null || dnf -y install git')

        chroot = self.cruncher.option('copr-chroot')
        if chroot.startswith('epel-6') or chroot.startswith('epel-7'):
            self.guest.run('rm -rf {1} && git clone {0} {1}'.format(url, self.source))
        else:
            self.guest.run('rm -rf {1} && git clone --depth 1 {0} {1}'.format(url, self.source))


        if chroot.startswith('epel-6'):
            self.guest.run('cd {} && git checkout {}'.format(self.source, ref))
        else:
            self.guest.run('cd {} && git fetch origin {}:ref && git checkout ref'.format(self.source, ref))

        self.info("[prepare] Using cloned repository as working directory on the test machine")
        self.guest.set_home(self.source)

    def download_yum_metadata(self):
        """ Create DNF cache: in case of flaky network, try it again """
        chroot = self.cruncher.option('copr-chroot')

        if chroot:
            # hack dnf for yum yuck (cruncher is dead anyway!)
            if chroot.startswith('epel-6') or chroot.startswith('epel-7'):
                self.info('[prepare] Hacking environment to work for CentOS 6/7')
                self.guest.run('ln -fs $(which yum) /usr/bin/dnf')
                self.guest.run('yum -y install yum-utils')

            # for epel first enable epel repo
            if chroot.startswith('epel-') or chroot.startswith('centos-'):
                self.info('[prepare] Enabling EPEL by default for CentOS/EPEL builds')
                self.guest.run('dnf -y install epel-release')

            # copr plugin for EPEL7
            if chroot.startswith('epel-7'):
                self.info('[prepare] Installing yum-plugin-copr for CentOS 7')
                self.guest.run('yum -y install yum-plugin-copr')

        if chroot.startswith('epel-') or chroot.startswith('centos-'):
            return

        count = 3
        while count > 0:
            try:
                self.guest.run('dnf makecache')
                break
            except gluetool.GlueError:
                count -= 1
                self.warn('[prepare] [{}/3] Unable to obtain repository metadata, trying again.'.format(3-count))
        else:
            # This is executed when the while conditions turns false
            raise gluetool.GlueError("We could not obtain repository metadata: this is an error.")

    def prepare(self):
        """ Prepare the guest for testing """
        # Prepare yum cache with retries
        self.download_yum_metadata()

        # Make sure we have downloaded FMF source to remote machine
        self.download_fmf()

        # Install copr build
        self.install_copr_build()

        # Handle the prepare step for ansible
        prepare = self.testset.get('prepare')
        if prepare and prepare.get('how') == 'ansible':
            self.info("[prepare] Installing Ansible requirements on test machine")
            self.guest.run('dnf -y install python', log='prepare.log')
            playbooks = prepare.get('playbooks')
            if not isinstance(playbooks, list):
                playbooks = [playbooks]
            for playbook in playbooks:
                self.info("[prepare] Applying Ansible playbook '{}'".format(playbook))
                self.guest.run_playbook(os.path.join(self.cruncher.fmf_root, playbook), log='prepare.log')

        # Prepare for beakerlib testing
        if self.testset.get(['execute', 'how']) == 'beakerlib':
            self.info('[prepare] Installing beakerlib and tests')
            self.guest.run('dnf -y install beakerlib git beakerlib-libraries', log='prepare.log')
            self.guest.run(
                'echo -e "#!/bin/bash\ntrue" > /usr/bin/rhts-environment.sh',
                log='prepare.log'
            ) # FIXME

    def execute_beakerlib_test(self, test):
        """ Execute a beakerlib test """
        self.info("[execute] Running test '{}'".format(test.name))
        # in case of remote tests, they are in 'tests' folder, otherwise they are in git source
        discover = self.testset.get('discover')
        if discover and discover.get('repository'):
            test_dir = os.path.join(self.remote_workdir, 'tests', (test.get('path') or test.name).lstrip('/'))
        else:
            test_dir = os.path.join(self.source, (test.get('path') or test.name).lstrip('/'))
        logs_dir = os.path.join(self.remote_workdir, 'logs', test.name.lstrip('/').replace('/', '-'))
        self.guest.run('cd {0}; mkdir -p {1}; BEAKERLIB_DIR={1} {2}'.format(
            test_dir, logs_dir, test.get('test')), log='execute.log')
        journal = self.guest.run('cat {}; sleep 1'.format(os.path.join(logs_dir, 'journal.txt')))
        self.info('journal: {}'.format(journal))
        if re.search('OVERALL RESULT: PASS', journal):
            self.info('overall is passed')
            result = 'passed'
        elif re.search('OVERALL RESULT: FAIL', journal):
            self.info('overall is failed')
            result = 'failed'
        else:
            result = 'error'

        return result

    def execute(self):
        """ Execute discovered tests """
        execute = self.testset.get('execute')

        # Shell
        if execute.get('how', 'shell') == 'shell':
            self.info('[execute] Running shell commands')

            # Currently we only support one script or a list of scripts
            # TODO: Add support for multi-line script as well (see L2 spec)
            if 'script' in execute:
                scripts = execute['script']
            elif 'commands' in execute:
                scripts = execute['commands']
                self.warn("The 'command' key was changed to 'script', please update your test.")
            else:
                raise gluetool.GlueError("No 'script' defined in the execute step.")

            if not isinstance(scripts, list):
                scripts = [scripts]

            try:
                for script in scripts:
                    self.guest.run(script, log='execute.log')
                result = 'passed'

            except gluetool.GlueError as error:
                self.error(error)
                result = 'failed'

            self.results.append({
                'name': self.testset.name,
                'result': result,
            })

        # Beakerlib
        if execute.get('how') == 'beakerlib':
            # Clone tests from remote location
            if self.testset.get(['discover', 'repository']):
                self.guest.run(
                    'rm -rf {0}; mkdir {0}; cd {0}; git clone {1} tests'.format(
                        self.remote_workdir,
                        str(self.testset.get(['discover', 'repository']))
                    )
                )
                self.info('[execute] Running beakerlib tests')

            # Execute tests and get results
            results = []
            for test in self.tests:
                results.append(self.execute_beakerlib_test(test))

            self.info(results)

            # Count overall result
            if 'failed' in results or 'error' in results:
                result = 'failed'
            else:
                result = 'passed'

            self.results.append({
                'name': self.testset.name,
                'result': result,
            })

    def report(self):
        """ Report results """
        return self.results

    def finish(self):
        """ Finishing tasks """

        # Archive remote workdir to artifacts
        if not self.guest:
            self.debug('no guest provisioned, nothing to cleanup')
            return

        try:
            self.guest.archive(self.remote_workdir, os.path.join(self.workdir, 'workdir'), log='finish.log')
        except:
            pass

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
            'artifact-root-url': {
                'help': 'Root of the URL to the artifacts',
            },
            'console-url': {
                'help': 'URL to console user interface.'
            },
            'artifacts-url': {
                'help': 'URL to artifacts interface.'
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
                'help': 'Use image from given URL',
                'default': 'https://dl.fedoraproject.org/pub/fedora/linux/releases/30/Cloud/x86_64/images/Fedora-Cloud-Base-30-1.2.x86_64.qcow2',
            },
            'image-file': {
                'help': 'Use image from give file'
            },
        }),
        ('Image download options', {
            'image-cache-dir': {
                'help': 'Image download path',
                'default': os.path.abspath('.')
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
                'help': 'Git reference to checkout',
                'default': 'master'
            },
            'git-commit-sha': {
                'help': 'Git commit SHA'
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
                'help': 'A globally unique ID which identifies the test.'
            },
            'post-results': {
                'help': 'Post results to URL defined in ``post-results-url``.',
                'action': 'store_true'
            },
            'post-results-token': {
                'help': 'Secret token injected into results json.'
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
            'artifacts-dir': {
                'help': 'Use given folder to store test artifacts (default: %(default)s)',
                'default': 'artifacts'
            }
        })
    ]

    def __init__(self, *args, **kwargs):
        super(Cruncher, self).__init__(*args, **kwargs)

        check_for_commands(REQUIRED_CMDS)

        self.guest = None
        self.image = None

        colorama.init(autoreset=True)

        self.testsets = []
        self.results = []

    @cached_property
    def artifacts_dir(self):
        return gluetool.utils.normalize_path(self.option('artifacts-dir'))

    @cached_property
    def image_cache_dir(self):
        return gluetool.utils.normalize_path(self.option('image-cache-dir'))

    def sanity(self):
        self.fmf_root = gluetool.utils.normalize_path_option(self.option('fmf-root'))
        # copr-chroot and copr-name are required
        if (self.option('copr-chroot') and not self.option('copr-name')) or \
                (self.option('copr-name') and not self.option('copr-chroot')):
            raise gluetool.utils.IncompatibleOptionsError(
               'Insufficient information about copr build supplied, both chroot and repository name need to be specified.')

        # make sure artifacts dir exists early
        try:
            os.makedirs(self.artifacts_dir)
        except OSError as e:
            # Ignore artifact dir already exists
            if e.errno not in [17]:
                raise gluetool.GlueError("Could not create artifacts directory '{}': {} ".format(self.artifacts_dir, str(e)))
        finally:
            os.chdir(self.artifacts_dir)

        # make sure image cache dir exists early
        try:
            os.makedirs(self.image_cache_dir)
        except OSError as e:
            # Ignore dir already exists
            if e.errno not in [17]:
                raise gluetool.GlueError("Could not create image cache directory '{}': {} ".format(self.image_cache_dir, str(e)))

    def image_from_url(self, url):
        """
        Maps copr chroot to a specific image.
        """
        image_name = os.path.basename(url)
        download_path = os.path.join(self.image_cache_dir, image_name)

        # check if image already exits
        if os.path.exists(download_path):
            return download_path

        self.info("Downloading image '{}'".format(url))

        command = ['curl']

        if self.option('no-progress'):
            command.append('-s')

        command.extend([
            '-fkLo', download_path,
            url
        ])

        try:
            Command(command).run(inspect=True, cwd=self.artifacts_dir)

        except gluetool.GlueCommandError as error:
            # make sure that we remove the image file
            os.unlink(download_path)

            raise gluetool.GlueError('Could not download image: {}'.format(error))

        return download_path

    def resolve_image(self):
        self.image = self.option('image-file')

        # Use image from path
        if self.image:
            self.info("Using image from file '{}'".format(self.image))
            return

        # Use image from url
        image_url = self.option('image-url')

        # Map image from copr repository
        if self.option('image-copr-chroot-map') and self.option('copr-chroot'):
            image_url = render_template(
                SimplePatternMap(
                    gluetool.utils.normalize_path(self.option('image-copr-chroot-map')),
                    logger=self.logger
                ).match(self.option('copr-chroot'))
            )

        # resolve image from URL
        if image_url:
            self.image = self.image_from_url(image_url)
            return

        if not image_url and not self.option('ssh-host'):
            raise gluetool.utils.IncompatibleOptionsError(
                'No image, SSH host or copr repository specified. Cannot continue.')

    def install_test(self):
        """ Runs Installation tast only """

        # resolve image
        self.resolve_image()

        class InstallTestSet(object):
            name = '/install/copr-build'

            def get(self, key):
                if key == 'execute':
                    return True
                if key == 'summary':
                    return 'Test copr build installation'

        install_test = TestSet(self, InstallTestSet(), cruncher=self)

        install_test.provision()

        try:
            install_test.install_copr_build()
            self.results.append({
                'name': '/install/copr-build',
                'result': 'passed',
            })

        except gluetool.GlueError:
            self.results.append({
                'name': '/install/copr-build',
                'result': 'failed',
            })

    def execute(self):
        """ Process all testsets defined """
        # Initialize the metadata tree
        git_url = self.option('git-url')
        git_ref = self.option('git-ref')
        # FIXME: get rid of workdir
        self.workdir = self.artifacts_dir

        if self.fmf_root:
            self.info("Getting FMF metadata from local path '{}' ".format(self.fmf_root))

        elif git_url and git_ref:
            self.info("Getting FMF metadata from git repository '{}' ref '{}' ".format(git_url, git_ref))
            Command(['rm', '-rf', 'source']).run(cwd=self.artifacts_dir)
            Command(['git', 'clone', '--depth=1', git_url, 'source' ]).run(cwd=self.artifacts_dir)
            Command(['git', 'fetch', 'origin', '{0}:ref'.format(git_ref)]).run(cwd=os.path.join(self.artifacts_dir, 'source'))
            Command(['git', 'checkout', 'ref']).run(cwd=os.path.join(self.artifacts_dir, 'source'))
            self.fmf_root = os.path.join(self.artifacts_dir, 'source')

        else:
            self.info("Nothing to do, no FMF metadata provided")
            return

        # init FMF tree
        try:
            tree = fmf.Tree(self.fmf_root)

            # resolve image
            self.resolve_image()

            # FIXME: blow up if no tests to run
            # FIXME: other checks for fmf validity?

            testsets = list(tree.prune(keys=['execute']))
            log_dict(self.info, "Discovered testsets", [testset.name for testset in testsets])

            for testset in testsets:
                self.testsets.append(TestSet(self, testset, cruncher=self))

            # Process each testset found in the fmf tree
            for testset in self.testsets:
                # Skip irrelevant testsets
                if not testset.relevant():
                    self.info("Skipping irrelevant testset {}".format(testset.name))
                    continue
                self.results.extend(testset.go())

        # no FMF tree - run copr build install test only
        except fmf.utils.RootError:
            # run installation only
            self.install_test()

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
            result = 'passed'

            failed = sum([r['result'] == 'failed' for r in self.results])

            if failed > 0:
                result = 'failed'
                result_count = len(self.results)
                message['message'] = '{} {} from {} failed'.format(
                    failed,
                    'plan' if result_count == 1 else 'plans',
                    result_count
                )

            else:
                message['message'] = 'All tests passed'

        message['result'] = result

        pipeline_id = self.option('pipeline-id')

        if pipeline_id:
            message.update({
                'pipeline': {
                    'id': pipeline_id
                },
                'token': self.option('post-results-token')
            })

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

        if self.option('console-url'):
            message.update({
                'url': '{}/pipeline/{}'.format(self.option('console-url'), pipeline_id),
            })

        # sanitize the message - remove the token
        message_sanitized = message.copy()
        message_sanitized['token'] = 'XXXXXXXXXXXXXXXXXXXXXXXXXX'
        log_dict(self.info, 'result message', message_sanitized)

        post_results_urls = gluetool.utils.normalize_multistring_option(self.option('post-results-url'))

        log_dict(self.info, 'posting result message to URLs', post_results_urls)

        for url in post_results_urls:
            with requests(logger=self.logger) as req:
                response = req.post(url, json=message)

            if response.status_code not in [200, 202]:
                raise gluetool.GlueError('Could not post results to "{}"'.format(url))

    def destroy(self, failure):
        """ Cleanup and notification tasks """
        # post results about testing
        self.post_results(failure)

        # run finish for all testsets
        for testset in self.testsets:
            testset.finish()

        # Remove workdir if requested
        if self.option('cleanup') and TestSet._workdir:
            self.info("Removing workdir '{}'".format(TestSet._workdir))
            shutil.rmtree(TestSet._workdir)
