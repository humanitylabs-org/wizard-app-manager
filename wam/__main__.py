import sys
from .docker_group import reexec_with_docker_group
reexec_with_docker_group()
from .cli import main
sys.exit(main())
