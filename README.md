Testing Farm - Flock prototype
==============================

This repository implements a simple Jenkins based runner of FMF based tests.

Development
-----------

0. Have a Fedora machine (Fedora 28+ advised)

1. Create virtualenv for python2 (according to your preference) and activate it

2. Install required packages

    `# dnf -y install curl ansible copr-cli qemu-kvm`

3. Install gluetool

    `$ pip install gluetool fmf`

Examples
--------

Execute simple tests and keep the vm instace for further testing:

    gluetool cruncher \
        --copr-chroot fedora-rawhide-x86_64 \
        --copr-name packit/packit-service-hello-world-8 \
        --fmf-root fmf/simple \
        --keep-instance

For fast execution use ssh options from the output to reuse the vm:

    gluetool cruncher \
        --copr-chroot fedora-rawhide-x86_64 \
        --copr-name packit/packit-service-hello-world-8 \
        --fmf-root fmf/simple \
        --ssh-host=127.0.0.3 \
        --ssh-key=/tmp/inventory-clouddec0ap__/identity \
        --ssh-port=3353
