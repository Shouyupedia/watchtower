## Prerequisites
To contribute code changes to this project you will need the following development kits.
 * [Go](https://golang.org/doc/install)
 * [Docker](https://docs.docker.com/engine/installation/)
 
The Go version to use is the one in `go.mod`; check yours with `go version`.


## Checking out the code
Do not place your code in the go source path.
```bash
git clone git@github.com:<yourfork>/watchtower.git
cd watchtower
```

## Building and testing
watchtower is a go application and is built with go commands. The following commands assume that you are at the root level of your repo.
```bash
go build                               # compiles and packages an executable binary, watchtower
go test ./... -v                       # runs tests with verbose output
./watchtower                           # runs the application (outside of a container)
```

### Integration tests
The tests in `test/integration` run watchtower against a real Docker daemon (the one `DOCKER_HOST` points to).
They create containers and images with names starting with `wtit-` and remove them afterwards, and need no registry.
```bash
go test -tags integration -count=1 -v ./test/integration/
```

If you dont have it enabled, you'll either have to prefix each command with `GO111MODULE=on` or run `export GO111MODULE=on` before running the commands. [You can read more about modules here.](https://github.com/golang/go/wiki/Modules)

To build a Watchtower image of your own, use the self-contained Dockerfiles. As the main Dockerfile, they can be found in `dockerfiles/`:
- `dockerfiles/Dockerfile.dev-self-contained` will build an image based on your current local Watchtower files.
- `dockerfiles/Dockerfile.self-contained` will build an image based on current Watchtower's repository on GitHub.

e.g.:
```bash
sudo docker build . -f dockerfiles/Dockerfile.dev-self-contained -t containrrr/watchtower # to build an image from local files
```