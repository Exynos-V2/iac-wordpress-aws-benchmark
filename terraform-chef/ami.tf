resource "aws_ami_from_instance" "wordpress" {
  name               = "${local.name_prefix}-wp-${formatdate("YYYYMMDDhhmmss", timestamp())}"
  source_instance_id = aws_instance.builder.id
  description        = "Golden WordPress AMI baked from configured builder (${var.stack_name} stack)."

  # Block until chef-client has finished writing all files.
  depends_on = [null_resource.chef_provision]

  tags = {
    Name = "${local.name_prefix}-wp-ami"
  }

  lifecycle {
    # name embeds a timestamp; ignore so re-applies don't try to recreate the AMI.
    ignore_changes = [name]
  }
}
