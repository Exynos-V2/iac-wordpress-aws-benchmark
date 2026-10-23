apt_update 'periodic 1h cache refresh' do
  frequency 3600
  action :periodic
end

%w(
  nginx
  php8.3-fpm
  php8.3-mysql
  php8.3-gd
  php8.3-xml
  php8.3-mbstring
  php8.3-curl
  php8.3-zip
  mysql-client
  nfs-common
  wget
  unzip
  rsync
  curl
).each do |pkg|
  apt_package pkg do
    action :install
  end
end
