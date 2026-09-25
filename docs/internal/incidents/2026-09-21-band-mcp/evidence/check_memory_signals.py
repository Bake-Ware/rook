import asyncio,logging,os,signal,secrets,tracemalloc
from rook.band_mcp.memory_diagnostics import install
async def main():
 logs=[]
 class Capture(logging.Handler):
  def emit(self,record):logs.append(record.getMessage())
 logger=logging.getLogger('rook.band_mcp.memory_diagnostics');h=Capture();logger.addHandler(h);logger.setLevel(logging.WARNING)
 uninstall=install()
 os.kill(os.getpid(),signal.SIGUSR1);await asyncio.sleep(.01)
 assert any('tracing is off' in x for x in logs)
 os.kill(os.getpid(),signal.SIGUSR2);await asyncio.sleep(.01)
 assert tracemalloc.is_tracing()
 marker=secrets.token_hex(32);payload=[marker+str(i) for i in range(100)]
 os.kill(os.getpid(),signal.SIGUSR1);await asyncio.sleep(.01)
 assert any('allocation site=' in x for x in logs)
 assert all(marker not in x for x in logs)
 os.kill(os.getpid(),signal.SIGUSR2);await asyncio.sleep(.01)
 assert not tracemalloc.is_tracing()
 uninstall();logger.removeHandler(h)
 print('SIGUSR1/SIGUSR2: off/enable/allocation-summary/disable verified; no object contents logged')
asyncio.run(main())
