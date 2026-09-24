import asyncio
import stackweave.aio as aio
loop = aio.StackweaveEventLoop(); asyncio.set_event_loop(loop)
out={}
async def main():
    try:
        loop.close(); out['closed']=True
    except RuntimeError as e: out['raised']=str(e)
    return 1
print(loop.run_until_complete(main()), out)
