# systemd 245 冒烟用的"考试机"：Ubuntu 20.04（NOI Linux 2.0 的底座）+ 真 systemd + Python 3.8.10。
# 由 scripts/smoke_systemd245.py 构建并以 --privileged 运行，PID 1 是 systemd。
# curl：评测机一键安装（install.sh）要它下载评测机程序。
FROM ubuntu:20.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends systemd systemd-sysv python3 procps curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && systemctl mask systemd-logind.service getty.target console-getty.service
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
