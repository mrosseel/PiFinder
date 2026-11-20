# The PiFinder update servers, read from python/PiFinder/update_servers.json:
# the one place that names them. The app (PiFinder/update_servers.py) and
# .github/scripts/update_manifest.py read the same file.
let
  servers = builtins.fromJSON (builtins.readFile ../python/PiFinder/update_servers.json);
in
{
  devCache = "${servers.attic_cache}/${servers.dev_namespace}";
  releaseCache = "${servers.attic_cache}/${servers.release_namespace}";
  deltaServer = servers.delta_server;
}
