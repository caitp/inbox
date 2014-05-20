#!/bin/sh

set -e

color() {
      printf '\033[%sm%s\033[m\n' "$@"
      # usage color "31;5" "string"
      # 0 default
      # 5 blink, 1 strong, 4 underlined
      # fg: 31 red,  32 green, 33 yellow, 34 blue, 35 purple, 36 cyan, 37 white
      # bg: 40 black, 41 red, 44 blue, 45 purple
      }

if [ ! -f ./config.cfg ]; then
    color '31;1' 'config.cfg file not found!'
    exit 1
fi

color '36;1' "
      _____       _
     |_   _|     | |
       | |  _ __ | |__   _____  __
       | | | '_ \| '_ \ / _ \ \/ /
      _| |_| | | | |_) | (_) >  <
     |_____|_| |_|_.__/ \___/_/\_\\

     This script installs dependencies for Inbox.

     For more details, visit:
     https://www.github.com/inboxapp/inbox
"

color '35;1' "Press enter to continue..."
read enterKey

color '35;1' 'Updating packages...'
apt-get update
apt-get -y install python-software-properties

# Preconfigure MySQL root password
echo "mysql-server mysql-server/root_password password root" | debconf-set-selections
echo "mysql-server mysql-server/root_password_again password root" | debconf-set-selections

color '35;1' 'Installing dependencies from apt-get...'
apt-get -y install git \
                   wget \
                   supervisor \
                   mysql-server \
                   mysql-client \
                   redis-server \
                   python \
                   python-dev \
                   python-pip \
                   python-setuptools \
                   build-essential \
                   libmysqlclient-dev \
                   gcc \
                   python-xapian \
                   libzmq-dev \
                   python-zmq \
                   libxml2-dev \
                   libxslt-dev \
                   lib32z1-dev \
                   libffi-dev \
                   python-lxml \
                   libmagickwand-dev \
                   tmux \
                   curl \
                   stunnel4

color '35;1' 'Installing dependencies from pip...'
pip install --upgrade setuptools
pip install -r requirements.txt

pip install -e src
if [ -d "../inbox-eas" ]; then
    pip install -e ../inbox-eas
fi

color '35;1' 'Finished installing dependencies.'
# mysql config
cp ./etc/my.cnf /etc/mysql/conf.d/inboxapp.cnf

mysqld_safe &
sleep 10

color '35;1' 'Creating databases...'
python tools/create_db.py
find . -name \*.pyc -delete

color '35;1' 'Setting up Redis...'
mkdir -p /etc/redis
chown $SUDO_USER /etc/redis

cp ./etc/redis.conf /etc/redis/inboxapp.conf

mkdir -p /var/redis/inboxapp/6379
chown $SUDO_USER /var/redis/inboxapp/6379

color '35;1' 'Cleaning up...'
apt-get -y purge build-essential
apt-get -y autoremove

mkdir -p /var/lib/inboxapp
chown $SUDO_USER /var/lib/inboxapp

mkdir -p /var/log/inboxapp
chown $SUDO_USER /var/log/inboxapp

color '35;1' 'Done!.'
