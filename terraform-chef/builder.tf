resource "aws_instance" "builder" {
  ami                         = data.aws_ami.ubuntu_2404.id
  instance_type               = var.builder_instance_type
  subnet_id                   = aws_subnet.public["a"].id
  vpc_security_group_ids      = [aws_security_group.builder.id]
  key_name                    = var.key_pair_name
  associate_public_ip_address = true
  iam_instance_profile        = var.instance_profile_name

  root_block_device {
    volume_size = 16
    volume_type = "gp3"
    encrypted   = true
  }

  tags = { Name = "${local.name_prefix}-builder" }

  depends_on = [
    aws_efs_mount_target.wp_content,
    aws_db_instance.wordpress,
  ]
}

# Render attributes.json from a template so the cookbook stays generic.
resource "local_file" "chef_attributes" {
  filename = "${path.module}/chef/attributes.json"
  content = templatefile("${path.module}/chef/attributes.json.tmpl", {
    rds_endpoint = aws_db_instance.wordpress.address
    db_name      = var.db_name
    db_user      = var.db_username
    db_password  = var.db_password
    efs_dns_name = aws_efs_file_system.wp_content.dns_name
    efs_fs_id    = aws_efs_file_system.wp_content.id
  })
  file_permission = "0600"
}

# Hash the cookbook so changes re-trigger provisioning.
locals {
  chef_cookbook_sha = sha1(join("", [
    filesha256("${path.module}/chef/solo.rb"),
    filesha256("${path.module}/chef/cookbooks/wordpress/metadata.rb"),
    filesha256("${path.module}/chef/cookbooks/wordpress/attributes/default.rb"),
    filesha256("${path.module}/chef/cookbooks/wordpress/recipes/default.rb"),
    filesha256("${path.module}/chef/cookbooks/wordpress/recipes/packages.rb"),
    filesha256("${path.module}/chef/cookbooks/wordpress/recipes/efs.rb"),
    filesha256("${path.module}/chef/cookbooks/wordpress/recipes/wordpress.rb"),
    filesha256("${path.module}/chef/cookbooks/wordpress/templates/wp-config.php.erb"),
    filesha256("${path.module}/chef/cookbooks/wordpress/templates/nginx-wordpress.conf.erb"),
  ]))
}

# Stamps the wall-clock moment just before Chef-side work begins (cookbook upload +
# chef-client install + recipe run). Paired with time_static.config_end to measure
# config_s in scripts/runs.py. Mirror of the Ansible stack's time_static.config_start.
resource "time_static" "config_start" {
  triggers = {
    builder_id     = aws_instance.builder.id
    cookbook_sha   = local.chef_cookbook_sha
    attributes_sha = local_file.chef_attributes.content_sha256
  }
  depends_on = [aws_instance.builder, local_file.chef_attributes]
}

# Gate on SSH, then upload the cookbook tree (mirrors Ansible's wait_for_ssh + push model).
resource "null_resource" "upload_chef" {
  depends_on = [aws_instance.builder, local_file.chef_attributes, time_static.config_start]

  triggers = {
    builder_id     = aws_instance.builder.id
    attributes_sha = local_file.chef_attributes.content_sha256
    cookbook_sha   = local.chef_cookbook_sha
  }

  connection {
    type        = "ssh"
    user        = "ubuntu"
    host        = aws_instance.builder.public_ip
    private_key = file(var.private_key_path)
    timeout     = "5m"
  }

  provisioner "remote-exec" {
    inline = [
      "echo ssh ready",
      "sudo rm -rf /tmp/chef",
    ]
  }

  # No trailing slash on source: scp copies the chef directory itself into
  # /tmp, producing /tmp/chef. (Trailing-slash form requires destination to
  # already exist as a directory and has historically been flaky.)
  provisioner "file" {
    source      = "${path.module}/chef"
    destination = "/tmp"
  }
}

resource "null_resource" "chef_provision" {
  depends_on = [null_resource.upload_chef]

  triggers = {
    builder_id     = aws_instance.builder.id
    attributes_sha = local_file.chef_attributes.content_sha256
    cookbook_sha   = local.chef_cookbook_sha
  }

  connection {
    type        = "ssh"
    user        = "ubuntu"
    host        = aws_instance.builder.public_ip
    private_key = file(var.private_key_path)
    timeout     = "15m"
  }

  provisioner "remote-exec" {
    inline = [
      # Install chef-client v18 from omnitruck if not already present.
      "if ! command -v chef-client >/dev/null 2>&1; then curl -L https://omnitruck.chef.io/install.sh | sudo bash -s -- -P chef -v 18; fi",
      "sudo chef-client --chef-license accept --local-mode --config /tmp/chef/solo.rb --json-attributes /tmp/chef/attributes.json --runlist 'recipe[wordpress::default]'",
    ]
  }
}

resource "time_static" "config_end" {
  triggers = {
    builder_id     = aws_instance.builder.id
    cookbook_sha   = local.chef_cookbook_sha
    attributes_sha = local_file.chef_attributes.content_sha256
  }
  depends_on = [null_resource.chef_provision]
}
