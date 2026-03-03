import asyncio
import logging
import os
from telebot.application.worker import TelebotWorker

async def main():
    logging.basicConfig(level=logging.INFO)
    try:
        print("🚀 Initializing TelebotWorker for REAL test run...")
        worker = TelebotWorker()
        
        print(f"Tasks found: {len(worker.settings.tasks)}")
        
        # We run all tasks to verify the user's request for 7 PDFs and 7 MDs
        for task in worker.settings.tasks:
            print(f"--- Executing Task: {task.get('name')} ---")
            await worker.run_task(task)
            
        print("\n✅ Execution complete. Checking reports directory...")
        files = os.listdir("reports")
        pdfs = [f for f in files if f.endswith(".pdf")]
        mds = [f for f in files if f.endswith(".md")]
        
        print(f"Found {len(pdfs)} PDFs and {len(mds)} MDs.")
        
    except Exception as e:
        print(f"❌ Error during execution: {e}")
        exit(1)

if __name__ == "__main__":
    asyncio.run(main())
