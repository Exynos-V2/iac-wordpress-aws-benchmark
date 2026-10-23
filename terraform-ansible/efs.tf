resource "aws_efs_file_system" "wp_content" {
  creation_token   = "${local.name_prefix}-wp-content"
  encrypted        = true
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }

  tags = {
    Name = "${local.name_prefix}-efs-wp-content"
  }
}

resource "aws_efs_mount_target" "wp_content" {
  for_each        = aws_subnet.private_data
  file_system_id  = aws_efs_file_system.wp_content.id
  subnet_id       = each.value.id
  security_groups = [aws_security_group.efs.id]
}
