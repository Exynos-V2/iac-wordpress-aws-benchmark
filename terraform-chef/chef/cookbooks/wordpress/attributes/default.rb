default['wordpress']['root']         = '/var/www/html'
default['wordpress']['content_dir']  = '/var/www/html/wp-content'
default['wordpress']['php_socket']   = '/run/php/php8.3-fpm.sock'

# Overridden via attributes.json rendered by Terraform.
default['wordpress']['db_host']      = 'localhost'
default['wordpress']['db_name']      = 'wordpress'
default['wordpress']['db_user']      = 'wpadmin'
default['wordpress']['db_password']  = 'changeme'
default['wordpress']['efs_dns_name'] = ''
default['wordpress']['efs_fs_id']    = ''
