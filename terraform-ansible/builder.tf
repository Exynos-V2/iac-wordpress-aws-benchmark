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

  # EFS mount targets and RDS must exist before the playbook can configure WP.
  depends_on = [
    aws_efs_mount_target.wp_content,
    aws_db_instance.wordpress,
  ]
}

# Gate on SSH responsiveness before kicking off the long-running playbook.
resource "null_resource" "wait_for_ssh" {
  triggers = { builder_id = aws_instance.builder.id }

  connection {
    type        = "ssh"
    user        = "ubuntu"
    host        = aws_instance.builder.public_ip
    private_key = file(var.private_key_path)
    timeout     = "5m"
  }

  provisioner "remote-exec" {
    inline = ["echo ssh ready"]
  }
}

resource "local_file" "ansible_inventory" {
  filename = "${path.module}/ansible/inventory.ini"
  content = templatefile("${path.module}/ansible/inventory.tmpl", {
    builder_ip       = aws_instance.builder.public_ip
    private_key_path = var.private_key_wsl_path
  })
  file_permission = "0600"
}

# Extra-vars passed to ansible via JSON file — avoids quoting hazards on Windows
# cmd → wsl → bash and keeps db_password out of the visible command line.
resource "local_file" "ansible_extra_vars" {
  filename = "${path.module}/ansible/extra_vars.json"
  content = jsonencode({
    rds_endpoint = aws_db_instance.wordpress.address
    db_name      = var.db_name
    db_user      = var.db_username
    db_password  = var.db_password
    efs_dns_name = aws_efs_file_system.wp_content.dns_name
    efs_fs_id    = aws_efs_file_system.wp_content.id
  })
  file_permission = "0600"
}

# Stamps the wall-clock moment just before Ansible runs. Paired with
# time_static.config_end to measure config_s in scripts/runs.py.
resource "time_static" "config_start" {
  triggers = {
    builder_id   = aws_instance.builder.id
    playbook_sha = filesha256("${path.module}/ansible/playbook.yml")
  }
  depends_on = [null_resource.wait_for_ssh]
}

resource "null_resource" "ansible_provision" {
  depends_on = [
    null_resource.wait_for_ssh,
    local_file.ansible_inventory,
    local_file.ansible_extra_vars,
    time_static.config_start,
  ]

  triggers = {
    builder_id     = aws_instance.builder.id
    playbook_sha   = filesha256("${path.module}/ansible/playbook.yml")
    wpconfig_sha   = filesha256("${path.module}/ansible/templates/wp-config.php.j2")
    nginxconf_sha  = filesha256("${path.module}/ansible/templates/nginx-wordpress.conf.j2")
    extra_vars_sha = local_file.ansible_extra_vars.content_sha256
  }

  provisioner "local-exec" {
    working_dir = "${path.module}/ansible"
    command     = "wsl ansible-playbook -i inventory.ini -u ubuntu --extra-vars @extra_vars.json playbook.yml"
  }
}

resource "time_static" "config_end" {
  triggers = {
    builder_id   = aws_instance.builder.id
    playbook_sha = filesha256("${path.module}/ansible/playbook.yml")
  }
  depends_on = [null_resource.ansible_provision]
}
