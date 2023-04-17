from src import worker
import click


@click.command()
@click.option('--address', default='localhost', help='Client address')
@click.option('--port', default=5560, type=int, help='Client port')
@click.option('--map', help='Graph map')
def main(address: str, port: int, map):
    w = worker.Worker(map, address=address, port=port)
    w.run()


if __name__ == '__main__':
    main()