resource "aws_launch_template" "wordpress" {
  name_prefix   = "${local.name_prefix}-lt-"
  image_id      = aws_ami_from_instance.wordpress.id
  instance_type = var.app_instance_type
  key_name      = var.key_pair_name

  iam_instance_profile {
    name = var.instance_profile_name
  }

  network_interfaces {
    associate_public_ip_address = false
    security_groups             = [aws_security_group.app.id]
  }

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "${local.name_prefix}-wp-asg"
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_autoscaling_group" "wordpress" {
  name                = "${local.name_prefix}-asg"
  desired_capacity    = var.desired_capacity
  min_size            = var.min_size
  max_size            = var.max_size
  vpc_zone_identifier = [for s in aws_subnet.private_app : s.id]
  target_group_arns   = [aws_lb_target_group.wordpress.arn]

  health_check_type         = "ELB"
  health_check_grace_period = 120

  # min_elb_capacity + wait_for_capacity_timeout make `terraform apply` block
  # until ASG instances pass ALB health checks. This is what makes the
  # single-command end-to-end timing meaningful for the thesis.
  min_elb_capacity          = var.desired_capacity
  wait_for_capacity_timeout = "10m"

  launch_template {
    id      = aws_launch_template.wordpress.id
    version = "$Latest"
  }

  tag {
    key                 = "Name"
    value               = "${local.name_prefix}-wp-asg"
    propagate_at_launch = true
  }

  lifecycle {
    create_before_destroy = true
  }
}
