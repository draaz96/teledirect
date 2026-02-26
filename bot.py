import os
import uuid
import asyncio
from aiohttp import web
from telethon import TelegramClient, events
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 8080))
HOST = os.getenv("HOST", "0.0.0.0")
BASE_URL = os.getenv("BASE_URL", f"http://localhost:{PORT}")

if not all([API_ID, API_HASH, BOT_TOKEN]):
    print("Error: API_ID, API_HASH, and BOT_TOKEN must be set in .env")
    exit(1)

# Initialize Telethon Client
client = TelegramClient('bot_session', API_ID, API_HASH)

# In-memory storage for file mappings (Task: Consider persistence for production)
file_map = {}

async def health_check(request):
    return web.Response(text="Bot is running and healthy!")

async def heartbeat():
    """Background task to ping the bot's own health check to prevent Render timeout."""
    import aiohttp
    print("Heartbeat started...")
    try:
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    async with session.get(f"{BASE_URL}/") as resp:
                        if resp.status == 200:
                            print("Heartbeat: Self-ping successful (Keeping Render awake)")
                        else:
                            print(f"Heartbeat: Self-ping failed with status {resp.status}")
                except Exception as e:
                    print(f"Heartbeat error: {e}")
                
                # Ping every 5 minutes (Render timeout is 15 mins)
                await asyncio.sleep(5 * 60)
    except asyncio.CancelledError:
        print("Heartbeat stopped.")

async def download_handler(request):
    file_id = request.match_info.get('uuid')
    if file_id not in file_map:
        return web.Response(text="File not found or expired", status=404)

    msg_id = file_map[file_id]['msg_id']
    chat_id = file_map[file_id]['chat_id']
    file_name = file_map[file_id]['file_name']
    file_size = file_map[file_id]['file_size']

    # Get the message object
    message = await client.get_messages(chat_id, ids=msg_id)
    if not message or not message.file:
        return web.Response(text="File no longer available", status=404)

    # Prepare streaming response
    response = web.StreamResponse(
        status=200,
        reason='OK',
        headers={
            'Content-Type': message.file.mime_type or 'application/octet-stream',
            'Content-Disposition': f'attachment; filename="{file_name}"',
            'Content-Length': str(file_size)
        }
    )

    await response.prepare(request)
    print(f"Starting download: {file_name} ({file_size / (1024*1024):.2f} MB)")

    # Start the heartbeat to keep Render awake during this download
    heartbeat_task = asyncio.create_task(heartbeat())

    try:
        bytes_sent = 0
        last_logged = 0
        # Stream the file from Telegram with optimized 4MB chunk size for max speed
        async for chunk in client.iter_download(message.media, request_size=4096 * 1024):
            await response.write(chunk)
            bytes_sent += len(chunk)
            
            # Log progress every 5MB
            if bytes_sent - last_logged > 5 * 1024 * 1024:
                print(f"Streaming {file_name}: {bytes_sent / (1024*1024):.2f} MB sent...")
                last_logged = bytes_sent

        await response.write_eof()
        print(f"Download complete: {file_name}")
    except (ConnectionResetError, ConnectionError, asyncio.CancelledError) as e:
        print(f"Client disconnected during download of {file_name}: {e}")
    except Exception as e:
        print(f"Unexpected error during download: {e}")
    finally:
        # Stop the heartbeat once download finishes or fails
        heartbeat_task.cancel()
    
    return response

@client.on(events.NewMessage)
async def handle_message(event):
    if event.is_private and (event.document or event.video or event.audio):
        file = event.document or event.video or event.audio
        file_name = getattr(file.attributes[0], 'file_name', 'downloaded_file') if hasattr(file, 'attributes') and file.attributes else 'downloaded_file'
        if not file_name or file_name == 'downloaded_file':
             # fallback for videos or audios
             for attr in file.attributes:
                 if hasattr(attr, 'file_name'):
                     file_name = attr.file_name
                     break
        
        unique_id = str(uuid.uuid4())
        file_map[unique_id] = {
            'msg_id': event.id,
            'chat_id': event.chat_id,
            'file_name': file_name,
            'file_size': file.size
        }

        # Build the link
        download_link = f"{BASE_URL}/download/{unique_id}"
        
        await event.reply(
            f"✅ **File Received!**\n\n"
            f"🔗 **Download Link:**\n`{download_link}`\n\n"
            f"💡 *Note: You can share this link with anyone!*"
        )

async def start_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/download/{uuid}', download_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    print(f"Streaming server started at http://{HOST}:{PORT}")

async def main():
    await start_server()
    print("Starting bot client...")
    await client.start(bot_token=BOT_TOKEN)
    print("Bot is running...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
