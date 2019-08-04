FROM fedora:latest

RUN dnf -y install python-pip curl ansible copr-cli git qemu-kvm fmf genisoimage git

RUN pip install gluetool fmf

RUN mkdir -p /etc/gluetool.d /opt/cruncher /opt/cruncher/images

COPY configuration/container/gluetool /etc/gluetool.d/gluetool
COPY configuration/container  /opt/cruncher/configuration
COPY modules  /opt/cruncher/modules
COPY tools  /opt/cruncher/tools

ENTRYPOINT ["/usr/bin/gluetool"]
