"""Start the UI and scheduler in the same process so settings apply live."""
import scheduler
from webui import start_webui

def main():
    instance = scheduler.Scheduler()
    start_webui(instance)
    instance.run_forever()

if __name__ == "__main__":
    main()
