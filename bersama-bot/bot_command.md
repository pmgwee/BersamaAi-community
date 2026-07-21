
## Kickoff the bot (systemd service)
== Done. Bot is running under systemd (auto-restarts on crash/reboot). ==
   status:  sudo systemctl status bersama
   logs:    tail -f /home/ngxiaohao123/BersamaAi-community/bersama-bot/bersama.log
   stop:    sudo systemctl stop bersama
   restart: sudo systemctl restart bersama


   tail -f bersama.log


## Git pull + restart (for updates)
   cd ~/BersamaAi-community/bersama-bot && git pull && sudo systemctl restart bersama && sleep 3 && tail -n 4 bersama.log
