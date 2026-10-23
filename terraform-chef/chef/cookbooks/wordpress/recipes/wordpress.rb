wp_root    = node['wordpress']['root']
php_socket = node['wordpress']['php_socket']

remote_file '/tmp/wordpress.tar.gz' do
  source 'https://wordpress.org/latest.tar.gz'
  mode   '0644'
  not_if { ::File.exist?("#{wp_root}/wp-settings.php") }
end

execute 'extract wordpress tarball' do
  command 'tar xzf /tmp/wordpress.tar.gz -C /tmp/'
  creates '/tmp/wordpress/wp-settings.php'
end

execute 'sync wordpress to document root' do
  command "rsync -a --exclude=wp-content/ /tmp/wordpress/ #{wp_root}/"
  creates "#{wp_root}/wp-settings.php"
end

# Seed EFS-mounted wp-content with bundled themes/plugins on first run only.
execute 'seed wp-content on EFS' do
  command "cp -a /tmp/wordpress/wp-content/. #{node['wordpress']['content_dir']}/"
  not_if { ::File.exist?("#{node['wordpress']['content_dir']}/index.php") }
end

# Cache salts on disk so re-runs are idempotent (matches Ansible behaviour).
execute 'fetch wp salts' do
  command 'curl -s https://api.wordpress.org/secret-key/1.1/salt/ -o /tmp/wp-salts.txt'
  creates '/tmp/wp-salts.txt'
end

template "#{wp_root}/wp-config.php" do
  source 'wp-config.php.erb'
  owner  'www-data'
  group  'www-data'
  mode   '0640'
  # Read the cached salts at converge time via lazy: avoids a ruby_block that
  # would report "updated" on every re-run. The template itself stays
  # idempotent (same salts file -> same rendered content -> 0 updated on re-run).
  variables(lazy {
    {
      db_name:     node['wordpress']['db_name'],
      db_user:     node['wordpress']['db_user'],
      db_password: node['wordpress']['db_password'],
      db_host:     node['wordpress']['db_host'],
      wp_salts:    ::File.read('/tmp/wp-salts.txt'),
    }
  })
end

# Idempotency: permission setup runs once on a fresh builder. The
# /etc/wp-perms-done sentinel makes any re-converge a no-op (0 resources
# updated), mirroring the Ansible playbook. First-run behaviour is unchanged.
execute 'chown wordpress tree' do
  command "chown -R www-data:www-data #{wp_root}"
  not_if { ::File.exist?('/etc/wp-perms-done') }
end

execute 'chmod dirs to 755' do
  command "find #{wp_root} -type d -exec chmod 755 {} +"
  not_if { ::File.exist?('/etc/wp-perms-done') }
end

# Exclude wp-config.php so the template's stricter 0640 mode is preserved
# (otherwise a blanket 644 clobbers it, causing a one-time re-converge change).
execute 'chmod files to 644' do
  command "find #{wp_root} -type f ! -name wp-config.php -exec chmod 644 {} +"
  not_if { ::File.exist?('/etc/wp-perms-done') }
end

file '/etc/wp-perms-done' do
  content "permissions applied\n"
end

file '/etc/nginx/sites-enabled/default' do
  action :delete
  notifies :reload, 'service[nginx]', :delayed
end

template '/etc/nginx/sites-available/wordpress' do
  source 'nginx-wordpress.conf.erb'
  owner  'root'
  group  'root'
  mode   '0644'
  variables(
    wp_root:    wp_root,
    php_socket: php_socket
  )
  notifies :reload, 'service[nginx]', :delayed
end

link '/etc/nginx/sites-enabled/wordpress' do
  to '/etc/nginx/sites-available/wordpress'
  notifies :reload, 'service[nginx]', :delayed
end

# Validate only when the nginx config actually changes, so a re-converge
# reports 0 updated (mirrors Ansible's changed_when:false on `nginx -t`).
execute 'validate nginx config' do
  command 'nginx -t'
  action :nothing
  subscribes :run, 'template[/etc/nginx/sites-available/wordpress]', :immediately
  subscribes :run, 'link[/etc/nginx/sites-enabled/wordpress]', :immediately
end

service 'php8.3-fpm' do
  action [:enable, :start]
end

service 'nginx' do
  action [:enable, :start]
end
