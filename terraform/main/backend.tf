/******************************************
  Remote backend configuration
 *****************************************/

terraform {
  backend "s3" {
    bucket  = "391281939159-terraform-backend"
    key     = "terraform_state"
    region  = "eu-west-1"
    profile = "operations"
  }
}
