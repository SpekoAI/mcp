variable "REGISTRY" {
  default = ""
}

variable "IMAGE_NAME" {
  default = "spekoai-mcp"
}

variable "TAG" {
  default = "latest"
}

group "default" {
  targets = ["mcp"]
}

target "mcp" {
  context    = "../.."
  dockerfile = "packages/mcp-server/Dockerfile"
  tags = [
    notequal("", REGISTRY) ? "${REGISTRY}/${IMAGE_NAME}:${TAG}" : "${IMAGE_NAME}:${TAG}",
    notequal("", REGISTRY) ? "${REGISTRY}/${IMAGE_NAME}:latest" : "${IMAGE_NAME}:latest",
  ]
  platforms = ["linux/amd64"]
  output = ["type=registry"]
  cache-from = [
    notequal("", REGISTRY) ? "type=registry,ref=${REGISTRY}/${IMAGE_NAME}:buildcache" : "",
  ]
  cache-to = [
    notequal("", REGISTRY) ? "type=registry,ref=${REGISTRY}/${IMAGE_NAME}:buildcache,mode=max" : "",
  ]
}

