@echo off
setlocal
rem Load DISCORD_TOKEN / DISCORD_GUILD_ID from the .env file next to this script
for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%~dp0.env") do set "%%a=%%b"
rem Absolute log path: the jar's baked-in application.properties uses a RELATIVE
rem ./target/logs/... which littered every repo Claude Code opened this server from.
java -Dlogging.file.name="%~dp0logs\discord-mcp.log" -jar "%~dp0discord-mcp-1.0.0.jar"
