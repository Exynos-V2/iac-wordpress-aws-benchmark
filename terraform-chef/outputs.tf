output "alb_dns_name" {
  description = "Public DNS name of the WordPress ALB. Open http://<this>/ to verify."
  value       = aws_lb.wordpress.dns_name
}

output "ami_id" {
  description = "ID of the baked golden WordPress AMI."
  value       = aws_ami_from_instance.wordpress.id
}

output "builder_public_ip" {
  description = "Public IP of the builder EC2 (kept around so destroys are clean; safe to ignore once apply succeeds)."
  value       = aws_instance.builder.public_ip
}

output "asg_name" {
  description = "Auto Scaling Group name (use to test self-healing by terminating an instance)."
  value       = aws_autoscaling_group.wordpress.name
}

output "rds_endpoint" {
  description = "RDS MySQL endpoint hostname."
  value       = aws_db_instance.wordpress.address
}

output "efs_dns_name" {
  description = "EFS file system DNS name (mounted at /var/www/html/wp-content)."
  value       = aws_efs_file_system.wp_content.dns_name
}

output "config_start_ts" {
  description = "Unix epoch captured right before Chef-side work begins (cookbook upload + chef-client install + recipe run). Consumed by scripts/runs.py to split tf_apply_s vs config_s."
  value       = time_static.config_start.unix
}

output "config_end_ts" {
  description = "Unix epoch captured right after the chef-client run finishes."
  value       = time_static.config_end.unix
}
