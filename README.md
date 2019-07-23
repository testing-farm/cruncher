Testing Farm - Flock prototype
==============================

This repository implements a simple Jenkins based runner of FMF based tests.

Development
-----------

0. Have a Fedora machine (Fedora 28+ advised)

1. Create virtualenv for python2 (according to your preference) and activate it

2. Install required packages

# dnf -y install curl ansible copr-cli qemu-kvm

3. Install gluetool

$ pip install gluetool fmf
