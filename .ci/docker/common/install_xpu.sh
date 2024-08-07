#!/bin/bash
set -xe
# Script used in CI and CD pipeline

# Intel® software for general purpose GPU capabilities.
# Refer to https://www.intel.com/content/www/us/en/developer/articles/tool/pytorch-prerequisites-for-intel-gpus.html

# Users should update to the latest version as it becomes available

function install_ubuntu() {
    . /etc/os-release
    if [[ ! " jammy " =~ " ${VERSION_CODENAME} " ]]; then
        echo "Ubuntu version ${VERSION_CODENAME} not supported"
        exit
    fi

    apt-get update -y
    apt-get install -y gpg-agent wget
    # To add the online network package repository for the GPU Driver LTS releases
    wget -qO - https://repositories.intel.com/gpu/intel-graphics.key \
        | gpg --yes --dearmor --output /usr/share/keyrings/intel-graphics.gpg
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/intel-graphics.gpg] \
        https://repositories.intel.com/gpu/ubuntu ${VERSION_CODENAME}/lts/2350 unified" \
        | tee /etc/apt/sources.list.d/intel-gpu-${VERSION_CODENAME}.list
    # To add the online network network package repository for the Intel Support Packages
    wget -O- https://apt.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB \
        | gpg --dearmor > /usr/share/keyrings/intel-for-pytorch-gpu-dev-keyring.gpg
    echo "deb [signed-by=/usr/share/keyrings/intel-for-pytorch-gpu-dev-keyring.gpg] \
        https://apt.repos.intel.com/intel-for-pytorch-gpu-dev all main" \
        | tee /etc/apt/sources.list.d/intel-for-pytorch-gpu-dev.list

    # Update the packages list and repository index
    apt-get update

    # The xpu-smi packages
    apt-get install -y flex bison xpu-smi
    # Compute and Media Runtimes
    apt-get install -y \
        intel-opencl-icd intel-level-zero-gpu level-zero \
        intel-media-va-driver-non-free libmfx1 libmfxgen1 libvpl2 \
        libegl-mesa0 libegl1-mesa libegl1-mesa-dev libgbm1 libgl1-mesa-dev libgl1-mesa-dri \
        libglapi-mesa libgles2-mesa-dev libglx-mesa0 libigdgmm12 libxatracker2 mesa-va-drivers \
        mesa-vdpau-drivers mesa-vulkan-drivers va-driver-all vainfo hwinfo clinfo
    # Development Packages
    apt-get install -y libigc-dev intel-igc-cm libigdfcl-dev libigfxcmrt-dev level-zero-dev
    # Install Intel Support Packages
    if [ -n "$XPU_VERSION" ]; then
        apt-get install -y intel-for-pytorch-gpu-dev-${XPU_VERSION}
    else
        apt-get install -y intel-for-pytorch-gpu-dev
    fi

    # Cleanup
    apt-get autoclean && apt-get clean
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
}

function install_centos() {
    dnf install -y 'dnf-command(config-manager)'
    dnf config-manager --add-repo \
        https://repositories.intel.com/gpu/rhel/8.6/production/2328/unified/intel-gpu-8.6.repo
    # To add the EPEL repository needed for DKMS
    dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-8.noarch.rpm
        # https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm

    # Create the YUM repository file in the /temp directory as a normal user
    tee > /tmp/oneAPI.repo << EOF
[oneAPI]
name=Intel® oneAPI repository
baseurl=https://yum.repos.intel.com/oneapi
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=https://yum.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB
EOF

    # Move the newly created oneAPI.repo file to the YUM configuration directory /etc/yum.repos.d
    mv /tmp/oneAPI.repo /etc/yum.repos.d

    # The xpu-smi packages
    dnf install -y flex bison xpu-smi
    # Compute and Media Runtimes
    dnf install -y \
        intel-opencl intel-media intel-mediasdk libmfxgen1 libvpl2\
        level-zero intel-level-zero-gpu mesa-dri-drivers mesa-vulkan-drivers \
        mesa-vdpau-drivers libdrm mesa-libEGL mesa-libgbm mesa-libGL \
        mesa-libxatracker libvpl-tools intel-metrics-discovery \
        intel-metrics-library intel-igc-core intel-igc-cm \
        libva libva-utils intel-gmmlib libmetee intel-gsc intel-ocloc hwinfo clinfo
    # Development packages
    dnf install -y --refresh \
        intel-igc-opencl-devel level-zero-devel intel-gsc-devel libmetee-devel \
        level-zero-devel
    # Install Intel® oneAPI Base Toolkit
    dnf install intel-basekit -y

    # Cleanup
    dnf clean all
    rm -rf /var/cache/yum
    rm -rf /var/lib/yum/yumdb
    rm -rf /var/lib/yum/history
}

function install_rhel() {
    . /etc/os-release
    if [[ "${ID}" == "rhel" ]]; then
        if [[ ! " 8.6 8.8 8.9 9.0 9.2 9.3 " =~ " ${VERSION_ID} " ]]; then
            echo "RHEL version ${VERSION_ID} not supported"
            exit
        fi
    elif [[ "${ID}" == "almalinux" ]]; then
        # Workaround for almalinux8 which used by quay.io/pypa/manylinux_2_28_x86_64
        VERSION_ID="8.6"
    fi

    dnf install -y 'dnf-command(config-manager)'
    # To add the online network package repository for the GPU Driver LTS releases
    dnf config-manager --add-repo \
        https://repositories.intel.com/gpu/rhel/${VERSION_ID}/lts/2350/unified/intel-gpu-${VERSION_ID}.repo
    # To add the online network network package repository for the Intel Support Packages
    tee > /etc/yum.repos.d/intel-for-pytorch-gpu-dev.repo << EOF
[intel-for-pytorch-gpu-dev]
name=Intel for Pytorch GPU dev repository
baseurl=https://yum.repos.intel.com/intel-for-pytorch-gpu-dev
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=https://yum.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB
EOF

    # The xpu-smi packages
    dnf install -y xpu-smi
    # Compute and Media Runtimes
    dnf install -y \
        intel-opencl intel-media intel-mediasdk libmfxgen1 libvpl2\
        level-zero intel-level-zero-gpu mesa-dri-drivers mesa-vulkan-drivers \
        mesa-vdpau-drivers libdrm mesa-libEGL mesa-libgbm mesa-libGL \
        mesa-libxatracker libvpl-tools intel-metrics-discovery \
        intel-metrics-library intel-igc-core intel-igc-cm \
        libva libva-utils intel-gmmlib libmetee intel-gsc intel-ocloc
    # Development packages
    dnf install -y --refresh \
        intel-igc-opencl-devel level-zero-devel intel-gsc-devel libmetee-devel \
        level-zero-devel
    # Install Intel Support Packages
    yum install -y intel-for-pytorch-gpu-dev intel-pti-dev

    # Cleanup
    dnf clean all
    rm -rf /var/cache/yum
    rm -rf /var/lib/yum/yumdb
    rm -rf /var/lib/yum/history
}

function install_sles() {
    . /etc/os-release
    VERSION_SP=${VERSION_ID//./sp}
    if [[ ! " 15sp4 15sp5 " =~ " ${VERSION_SP} " ]]; then
        echo "SLES version ${VERSION_ID} not supported"
        exit
    fi

    # To add the online network package repository for the GPU Driver LTS releases
    zypper addrepo -f -r \
        https://repositories.intel.com/gpu/sles/${VERSION_SP}/lts/2350/unified/intel-gpu-${VERSION_SP}.repo
    rpm --import https://repositories.intel.com/gpu/intel-graphics.key
    # To add the online network network package repository for the Intel Support Packages
    zypper addrepo https://yum.repos.intel.com/intel-for-pytorch-gpu-dev intel-for-pytorch-gpu-dev
    rpm --import https://yum.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB

    # The xpu-smi packages
    zypper install -y lsb-release flex bison xpu-smi
    # Compute and Media Runtimes
    zypper install -y intel-level-zero-gpu level-zero intel-gsc intel-opencl intel-ocloc \
        intel-media-driver libigfxcmrt7 libvpl2 libvpl-tools libmfxgen1 libmfx1
    # Development packages
    zypper install -y libigdfcl-devel intel-igc-cm libigfxcmrt-devel level-zero-devel

    # Install Intel Support Packages
    zypper install -y intel-for-pytorch-gpu-dev intel-pti-dev

}


# The installation depends on the base OS
ID=$(grep -oP '(?<=^ID=).+' /etc/os-release | tr -d '"')
case "$ID" in
    ubuntu)
        install_ubuntu
    ;;
    centos)
        install_centos
    ;;
    rhel|almalinux)
        install_rhel
    ;;
    sles)
        install_sles
    ;;
    *)
        echo "Unable to determine OS..."
        exit 1
    ;;
esac
