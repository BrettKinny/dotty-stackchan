# bridge

## Build + Run
From the repo's root run the below commands to build the docker image and run it.
```console
$ docker build -t dotty-bridge:0.1.0 -f bridge/Dockerfile
$ docker compose -f bridge/docker-compose.yml up -d
```