directory node['wordpress']['content_dir'] do
  owner     'www-data'
  group     'www-data'
  mode      '0755'
  recursive true
end

mount node['wordpress']['content_dir'] do
  device    "#{node['wordpress']['efs_dns_name']}:/"
  fstype    'nfs4'
  options   'nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport,_netdev'
  action    [:mount, :enable]
end
