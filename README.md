Testing Farm - Cruncher
=======================

This repository implements a simple runner of FMF based tests in a qemu-kvm VM provisioned on the localhost.

The runner is distributed as a container image and can be found here:

    https://quay.io/repository/testing-farm/cruncher

Currently you must use docker to run it. Podman cannot be used due to this issue: https://github.com/containers/libpod/pull/3593.


Development
-----------

0. Have a recent Fedora machine (tested on F28+)

1. Create virtualenv for python2 (according to your preference) and activate it

2. Install required packages

    `# dnf -y install curl ansible copr-cli qemu-kvm genisoimage git`

3. Install gluetool

    `$ pip install gluetool fmf`


Examples of execution
---------------------

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


Execute as a container
----------------------

Running from our Quay.io repository

    docker run --privileged -v $(pwd)/images:/opt/cruncher/images:Z -v $(pwd)/fmf:/opt/cruncher/fmf:Z quay.io/testing-farm/cruncher cruncher --copr-chroot fedora-29-x86_64 --copr-name packit/packit-service-hello-world-8 --fmf-root /opt/cruncher/fmf/systemd

First build the container

    docker build -t cruncher . 

The container runs qemu-kvm, thus needs privileged containers.

    docker run --privileged -v $(pwd)/images:/opt/cruncher/images:Z -v $(pwd)/fmf:/opt/cruncher/fmf:Z cruncher:latest cruncher --copr-chroot fedora-29-x86_64 --copr-name packit/packit-service-hello-world-8 --fmf-root /opt/cruncher/fmf/simple


