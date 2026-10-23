locals {
  name_prefix = "${var.project_name}-${var.stack_name}"

  az_names = {
    a = var.azs[0]
    b = var.azs[1]
  }

  public_subnets = {
    a = { az = var.azs[0], cidr = "10.0.1.0/24" }
    b = { az = var.azs[1], cidr = "10.0.2.0/24" }
  }

  private_app_subnets = {
    a = { az = var.azs[0], cidr = "10.0.11.0/24" }
    b = { az = var.azs[1], cidr = "10.0.12.0/24" }
  }

  private_data_subnets = {
    a = { az = var.azs[0], cidr = "10.0.21.0/24" }
    b = { az = var.azs[1], cidr = "10.0.22.0/24" }
  }
}
