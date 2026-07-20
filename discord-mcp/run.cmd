@echo off
setlocal
rem Load DISCORD_TOKEN / DISCORD_GUILD_ID from the .env file next to this script
for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%~dp0.env") do set "%%a=%%b"
java -jar "%~dp0discord-mcp-1.0.0.jar"
