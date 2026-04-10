# Variables to sync permissions
export UID=$(shell id -u)
export GID=$(shell id -g)

start:
	docker compose up -d --build

stop:
	docker compose down

logs:
	tail -f output/container.log

status:
	docker compose ps

clean:
	docker compose down
	rm -rf output/container.log