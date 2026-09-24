# Left 4 Dead 2 Dedicated Server

A private Left 4 Dead 2 server for our group, set up with one script. The script installs the game server, keeps it running in the background, locks down the firewall, and gives you an `l4d2` command for everything afterward, including installing custom maps straight from the Steam Workshop.

## Requirements

- **Ubuntu 22.04 or 24.04** (or an Ubuntu-based distro like Mint), on a 64-bit Intel or AMD machine. ARM machines such as a Raspberry Pi or Oracle's free ARM servers won't work, because the game server only runs on x86.
- **At least 2 GB of RAM and 20 GB of free disk space.** The server download is roughly 10 GB, and custom maps add more.
- **A user account with sudo access.**

This works on a spare PC at home or on a cloud server (VPS). For players in the Philippines, a VPS in Singapore gives the best ping.

## Quick start

Copy `l4d2-setup.sh` to the machine and run:

```bash
sudo bash l4d2-setup.sh
```

The script asks a few questions, then shows a progress bar while it installs. The download is the slow part. When it finishes, a summary screen shows how friends connect. You can see it again anytime with `l4d2 info`.

### The setup questions

| Question | What to pick |
|---|---|
| Server name | Anything. It shows in the server browser. |
| Server password | Recommended. Friends need it to join. Leave blank for none. |
| RCON password | A random one is filled in. It lets you run admin commands from inside the game, and the optional [Telegram bot](#telegram-bot) needs it. Clear it to turn remote admin off. |
| Game mode | Co-op, Realism, or Versus. |
| Starting campaign | Which map the server loads when it starts. |
| Difficulty | Easy through Expert. |
| Network | How friends reach the server. See below. |
| Workshop maps | Optional. Paste Steam Workshop links to install maps during setup. |

### Choosing a network mode

**Tailscale (recommended).** Friends connect through Tailscale, a free app that creates a private link between their PC and the server. Nothing is exposed to the internet, and it works even if your ISP uses CGNAT, which is common in the Philippines. Near the end of setup you'll get a login link to open in your browser.

**Public.** The game port is open to the internet. This works immediately on a VPS. At home, you also need to forward **UDP port 27015** on your router to the server, and that won't work if your ISP uses CGNAT.

**Local network only.** Only devices on the same home network can join.

You can switch modes later by running the setup script again.

## Managing the server

Type `l4d2` on its own to open a menu with everything below. You can also run commands directly:

| Command | What it does |
|---|---|
| `l4d2` or `l4d2 menu` | Interactive menu |
| `l4d2 status` | Shows whether the server is running and its current settings |
| `l4d2 start` / `stop` / `restart` | Controls the server. Restarting disconnects anyone playing. |
| `l4d2 console` | Opens the live server console. Detach with **Ctrl+B** then **D**. Don't type `quit`, since that stops the server. |
| `l4d2 logs` | Follows the server log. Press **Ctrl+C** to exit. |
| `l4d2 config` | Edits `server.cfg`, then offers to restart. |
| `l4d2 update` | Updates the game files and any Workshop maps. |
| `l4d2 info` | Shows connection details for friends. |
| `l4d2 help` | Lists all commands. |

The server starts automatically when the machine boots and restarts itself if it crashes.

## Telegram bot

An optional bot that controls the server from a Telegram group: check who's
playing, change maps, install Workshop maps, restart, and run admin commands
from your phone.

It only makes outgoing connections, so nothing new is exposed to the internet
and it works fine behind Tailscale or CGNAT.

> **Everyone in the group gets full control of the game server,** including
> cheats and raw console commands. Use a private group, don't share the invite
> link, and set "Add members" to admins only.

### Setting it up

Copy the whole project folder to the server (not just the one script), then:

```bash
sudo bash telegram-bot-setup.sh
```

It walks you through creating a bot with [@BotFather](https://t.me/BotFather),
then waits while you add that bot to your group — it detects the group by
itself, so you don't need to type anything in the chat. It also turns on RCON
if you left it disabled during the main setup.

If it can't find the group, it offers to let you type the chat id in by hand.
Group ids are negative, like `-1001234567890`.

### Commands

| Command | What it does |
|---|---|
| `/status` `/players` `/map` | Is it up, who's on, what map |
| `/maps` `/info` `/share` | Installed maps, how to connect, links for friends |
| `/logs [n]` | Last lines of the server log |
| `/start` `/stop` `/restart` | Control the server |
| `/changemap <map> [default]` | Switch map now, optionally make it the default |
| `/addmap <link>` `/removemap <id>` `/updatemaps` | Manage Workshop maps |
| `/update` | Update game files (slow, server goes down) |
| `/cheats on\|off` `/difficulty <level>` | Live game settings |
| `/say <text>` `/kick <player>` | Talk to and moderate players |
| `/rcon <command>` | Run any console command |
| `/help` | The list above, in chat |

Anything that would disconnect players asks for confirmation first — but only
when someone is actually playing, so the prompt stays meaningful.

Long jobs like `/addmap` and `/update` run in the background and report
progress, one at a time.

### Managing the bot

```bash
systemctl status l4d2-telegram      # is it running
journalctl -u l4d2-telegram -f      # follow its log
sudo bash telegram-bot-setup.sh     # change the token or group
sudo bash telegram-bot-setup.sh --uninstall
```

### Tests

The bot has a test suite that needs no game server and no dependencies:

```bash
python3 -m unittest discover -s tests -t .
```


## Custom maps

### Adding maps

Paste a Steam Workshop link or ID. Collection links work too and install every map in the collection:

```bash
l4d2 addmap https://steamcommunity.com/sharedfiles/filedetails/?id=123456789
```

The server downloads the map, reads the campaign name and first map from inside the file, and asks whether to restart so the map loads. You can also install a `.vpk` or `.zip` file that's already on the server:

```bash
l4d2 addmap /path/to/map.vpk
```

### Playing a custom map

Switch maps with the menu, or by name:

```bash
l4d2 changemap deadcity_m1
```

Add `--default` to make the server start on that map from now on. Run `l4d2 maps` to see installed maps and their start map names.

### Everyone needs the same maps

Left 4 Dead 2 doesn't download custom maps to players automatically. Anyone missing a map gets kicked when the server switches to it. Run `l4d2 share` for a list of Workshop links to send your friends, and have them subscribe to each one.

### Other map commands

| Command | What it does |
|---|---|
| `l4d2 maps` | Lists installed custom maps |
| `l4d2 removemap <id or map name>` | Uninstalls a map |
| `l4d2 updatemaps` | Re-downloads Workshop maps that have been updated |
| `l4d2 share` | Lists map links to send friends |

## How friends join

Send each friend the **join guide page**, which walks them through setup step by step, along with:

1. **The Tailscale invite.** In the [Tailscale admin console](https://login.tailscale.com/admin/machines), click the **…** menu next to the server, choose **Share**, and send them the link. Each friend uses their own free Tailscale account and only gets access to the game server.
2. **The server address and password,** from `l4d2 info`.
3. **Map links,** from `l4d2 share`, if you've added custom maps.

In short, friends install Tailscale, accept the invite, enable the developer console in L4D2 (**Options → Keyboard/Mouse → Allow Developer Console**), press `~`, and type:

```
password YOURPASSWORD
connect 100.x.x.x:27015
```

**If you're the host**, you need Tailscale on your gaming PC too, signed in with the same account as the server. The exception is if you're playing on the server machine itself, in which case you can use `connect 127.0.0.1:27015`.

**To let devices on your home network join without Tailscale**, run:

```bash
sudo ufw allow from 192.168.0.0/16 to any port 27015 proto udp
```

## Troubleshooting

**Setup failed.** The error screen shows the last lines of the log. The full log is at `/var/log/l4d2-setup.log`. It's safe to fix the problem and run the script again.

**"The repository ... does not have a Release file."** This means an unrelated software source on your system is broken. The script continues past this, but it's worth cleaning up. For example, to remove the old Ookla Speedtest source:

```bash
sudo rm $(grep -rl "ookla" /etc/apt/sources.list.d/)
sudo apt-get update
```

**Friends can't connect.** Check that the server is running with `l4d2 status`. In Tailscale mode, make sure the friend's Tailscale app is on and your server appears in their list of machines. In public mode at home, check your port forward and whether your ISP uses CGNAT. The easy way to check for CGNAT is to compare the WAN IP in your router with the IP shown on whatismyip.com. If they differ, switch to Tailscale.

**The server won't start.** Check `l4d2 logs` or the log file at `/home/l4d2/l4d2server/left4dead2/console.log`.

**A Workshop map won't download.** Subscribe to it on your own PC, launch L4D2 once, then copy the file from `Steam\steamapps\common\Left 4 Dead 2\left4dead2\addons\workshop\` to the server and run `l4d2 addmap /path/to/file.vpk`.

**Players get kicked for a missing map.** They're missing a map or have an older version. Send them the links from `l4d2 share`.

**Setup says it didn't see the bot appear in any chat.** Make sure you added
the bot to a *group*, rather than only opening a direct chat with it. If the bot
was already in the group before you ran setup, send `/start@YourBot` in the
group so there's something for it to notice. A direct chat works too, and setup
will offer it.

**The Telegram bot doesn't answer.** Check `systemctl status l4d2-telegram`
and `journalctl -u l4d2-telegram -n 50`. The usual causes are a wrong group id,
or Telegram having upgraded your group to a supergroup, which changes its id —
re-run `telegram-bot-setup.sh` to pick the group again.

**The bot answers, but `/players` and `/cheats` don't work.** If it says RCON
was *rejected*, the password is wrong or unset — re-run `telegram-bot-setup.sh`,
which offers to enable it, or set `rcon_password` in `server.cfg` yourself and
restart the server.

If it says *connection refused*, the game server binds RCON to whatever the
machine's hostname resolves to, which on Ubuntu is usually `127.0.1.1` rather
than `127.0.0.1`. The bot tries both automatically. To see where it actually
listens:

```bash
ss -lnt 'sport = :27015'
```

Then set `L4D2_RCON_HOST` in `/etc/l4d2-telegram.env` to that address and run
`sudo systemctl restart l4d2-telegram`. If nothing is listening at all, the
server is down — check `l4d2 status`.

**"Conflict: terminated by other getUpdates request".** Two copies of the bot are
running against the same token, or the token has leaked. Stop any manual copy,
and revoke the token in @BotFather if you didn't start a second one.

## Security

The script sets things up to be reasonably safe by default:

- The server runs as its own `l4d2` user with no admin rights, so a problem in the game server can't reach the rest of the system.
- The firewall blocks all incoming traffic except SSH and the game port. In Tailscale mode, the game port only accepts connections through Tailscale.
- Only UDP 27015 is used for the game. The TCP admin port stays closed.

To keep it that way:

- Keep Ubuntu updated with `sudo apt update && sudo apt upgrade`, or turn on automatic security updates.
- Use a server password, and keep the RCON password private or leave it disabled.
- Only install maps and plugins from sources you trust.
- If you use the Telegram bot, keep the group private. Group membership is
  the only thing standing between someone and full control of the server.
  If the bot token leaks, open @BotFather, send `/revoke`, and re-run
  `telegram-bot-setup.sh` with the new token.
- On a VPS, log in with SSH keys and turn off password login.

## Where things live

| Path | What it is |
|---|---|
| `/home/l4d2/l4d2server/` | Game server files |
| `/home/l4d2/l4d2server/left4dead2/cfg/server.cfg` | Server settings |
| `/home/l4d2/l4d2server/left4dead2/addons/` | Installed maps and addons |
| `/home/l4d2/l4d2server/left4dead2/console.log` | Server log |
| `/home/l4d2/maps.json` | List of maps installed with `l4d2 addmap` |
| `/home/l4d2/SERVER-INFO.txt` | Connection details, shown by `l4d2 info` |
| `/etc/l4d2.env` | Default map, game mode, and paths |
| `/etc/systemd/system/l4d2.service` | Background service definition |
| `/usr/local/bin/l4d2` | The `l4d2` management command |
| `/var/log/l4d2-setup.log` | Setup log |
| `/opt/l4d2-telegram/` | Telegram bot code |
| `/etc/l4d2-telegram.env` | Bot token and group id (secret, mode 600) |
| `/etc/systemd/system/l4d2-telegram.service` | Bot service definition |
| `/var/lib/l4d2-telegram/` | Bot state |
| `/var/log/l4d2-telegram-setup.log` | Bot setup log |

## Uninstalling

If you installed the Telegram bot, remove it first:

```bash
sudo bash telegram-bot-setup.sh --uninstall
```

Then the server itself:

```bash
sudo systemctl disable --now l4d2
sudo rm /etc/systemd/system/l4d2.service /etc/l4d2.env /usr/local/bin/l4d2
sudo systemctl daemon-reload
sudo userdel -r l4d2
sudo ufw delete allow in on tailscale0 to any port 27015 proto udp
sudo ufw delete allow 27015/udp
```

The last two lines remove the firewall rules. One of them will say the rule doesn't exist, depending on which network mode you used. If you used local network mode, run `sudo ufw status numbered` and delete the port 27015 rules with `sudo ufw delete <number>`, starting from the highest number. To remove Tailscale as well, run `sudo apt remove tailscale`.
