/**
 * In-game admin shortcuts for a friends' co-op server.
 *
 * Every command works from chat ("!tank") and from the game console
 * ("sm_tank"), for SourceMod admins only. "!extras" opens a menu with all of
 * them.
 *
 *   !extras                 menu with everything below
 *   !difficulty [level]     easy | normal | advanced | expert (no level: menu)
 *   !mode [name]            co-op, realism or a mutation, then reloads the map
 *   !restartmap             restart the current chapter
 *   !tank  !witch  !horde   spawn one now
 *   !tankrun [on|off]       keep tanks coming, map after map, until turned off
 *   !survivors <number>     survivor team size; bots fill the empty seats
 *   !reverseff [on|off]     friendly fire hurts the shooter, not the teammate
 *   !give <item> [who]      a weapon or item: rifle_ak47, katana, molotov...
 *   !kit  !heal  !ammo [who]   first aid kit, full health, full ammo
 *   !laser [who]            laser sight on the weapon in hand
 *   !respawn [who]          bring a dead survivor back next to a living one
 *
 * [who] is a name (part of one is enough), #userid, @me, @all, @bots or
 * @humans; left out, it is whoever typed the command.
 *
 * Without a command: when a co-op finale is won, the server moves on to the
 * next official campaign instead of emptying (l4d2_extras_nextcampaign 0 turns
 * that off).
 */
#pragma semicolon 1
#pragma newdecls required

#include <sourcemod>
#include <sdktools>

#define TEAM_SURVIVOR 2
#define TEAM_INFECTED 3
#define NORMAL_TEAM 4
#define ZOMBIECLASS_TANK 8
#define TAG "\x04[Admin]\x01 "

// From the CreateSurvivorBot plugin that comes with MultiSlots. Optional: on a
// 4-player server neither is installed and !survivors says so.
native int CreateSurvivorBot();
// From Left 4 DHooks Direct, which MultiSlots needs too. Optional the same way.
native void L4D_RespawnPlayer(int client);
// Also from Left 4 DHooks: what kind of game a mode is underneath its name
// (1 co-op, 2 versus, 4 survival, 8 scavenge), mutations included.
native int L4D_GetGameModeType();

public Plugin myinfo =
{
	name = "L4D2 admin extras",
	author = "l4d2-server toolkit",
	description = "Chat commands for difficulty, game mode, tanks and hordes.",
	version = "1.0",
	url = ""
};

static const char g_sDifficulty[][][] =
{
	{ "Easy", "Easy" },
	{ "Normal", "Normal" },
	{ "Hard", "Advanced" },
	{ "Impossible", "Expert" }
};

static const char g_sModes[][][] =
{
	{ "coop", "Co-op" },
	{ "realism", "Realism" },
	{ "mutation4", "Hard Eight (8 special infected)" },
	{ "mutation2", "Headshot!" },
	{ "mutation3", "Bleed Out" },
	{ "mutation8", "Ironman" },
	{ "mutation16", "Hunting Party" },
	{ "community1", "Special Delivery" },
	{ "community5", "Death's Door" }
};

// The official campaigns by their first chapter, in the order the game numbers
// them. After a finale the next one in this list is loaded.
static const char g_sCampaigns[][][] =
{
	{ "c1m1_hotel", "Dead Center" },
	{ "c2m1_highway", "Dark Carnival" },
	{ "c3m1_plankcountry", "Swamp Fever" },
	{ "c4m1_milltown_a", "Hard Rain" },
	{ "c5m1_waterfront", "The Parish" },
	{ "c6m1_riverbank", "The Passing" },
	{ "c7m1_docks", "The Sacrifice" },
	{ "c8m1_apartment", "No Mercy" },
	{ "c9m1_alleys", "Crash Course" },
	{ "c10m1_caves", "Death Toll" },
	{ "c11m1_greenhouse", "Dead Air" },
	{ "c12m1_hilltop", "Blood Harvest" },
	{ "c13m1_alpinecreek", "Cold Stream" },
	{ "c14m1_junkyard", "The Last Stand" }
};

// The eight survivors, in the order of the game's character numbers.
static const char g_sSurvivorNames[][] = { "Nick", "Rochelle", "Coach", "Ellis", "Bill", "Zoey", "Francis", "Louis" };
static const char g_sSurvivorModels[][] =
{
	"models/survivors/survivor_gambler.mdl",
	"models/survivors/survivor_producer.mdl",
	"models/survivors/survivor_coach.mdl",
	"models/survivors/survivor_mechanic.mdl",
	"models/survivors/survivor_namvet.mdl",
	"models/survivors/survivor_teenangst.mdl",
	"models/survivors/survivor_biker.mdl",
	"models/survivors/survivor_manager.mdl"
};

ConVar g_hDifficulty;
ConVar g_hGameMode;
ConVar g_hTankRunInterval;
ConVar g_hTankRunMax;
ConVar g_hTankRunState;
Handle g_hTankRunTimer;
// A tank run lasts across map changes until it is turned off or the server
// restarts. On each map (and after a wipe) the tanks wait until a survivor
// has left the safe room, so nobody gets a tank on top of them while loading.
bool g_bTankRun;
bool g_bLeftStart;

// Survivor team size asked for with !survivors (0: never asked). MultiSlots
// owns the team: it kicks bots above its "min survivors" and its config puts
// that back to 4 on every map load. So the size is kept here, written to
// MultiSlots again after the configs have run, and forgotten on a restart.
ConVar g_hSurvivors;
Handle g_hTeamTimer;
int g_iTeamTarget;
int g_iTeamTries;

// server.cfg sets z_difficulty and the game runs it again on every map load,
// which would undo a difficulty picked in game. So the last difficulty chosen
// while a map was running (here, by a vote, or from the web panel) is kept and
// put back once the configs have run. A server restart forgets it.
char g_sWantedDifficulty[32];
bool g_bConfigsRunning = true;

// Reverse friendly fire belongs to the l4d_reverse_ff plugin, whose own config
// sets it again on every map load. So what !reverseff last asked for (-1:
// never asked) is kept here the same way and put back once the configs have run.
int g_iWantedReverseFF = -1;

ConVar g_hNextCampaign;
ConVar g_hNextCampaignDelay;

public APLRes AskPluginLoad2(Handle myself, bool late, char[] error, int err_max)
{
	g_bLeftStart = late;   // loaded mid-game: the survivors are most likely out already
	MarkNativeAsOptional("CreateSurvivorBot");
	MarkNativeAsOptional("L4D_RespawnPlayer");
	MarkNativeAsOptional("L4D_GetGameModeType");
	return APLRes_Success;
}

public void OnPluginStart()
{
	g_hDifficulty = FindConVar("z_difficulty");
	g_hGameMode = FindConVar("mp_gamemode");
	g_hTankRunInterval = CreateConVar("l4d2_extras_tankrun_interval", "25", "Tank run: seconds between tank spawns.", _, true, 5.0, true, 300.0);
	g_hTankRunMax = CreateConVar("l4d2_extras_tankrun_max", "2", "Tank run: most tanks alive at once.", _, true, 1.0, true, 8.0);
	// Follows the tank run, so the web panel can read whether one is on; setting
	// it does the same as sm_tankrun on/off.
	g_hTankRunState = CreateConVar("l4d2_extras_tankrun", "0", "Tank run: 1 = on, 0 = off. Lasts across maps until turned off or the server restarts.", _, true, 0.0, true, 1.0);
	g_hTankRunState.AddChangeHook(OnTankRunStateChanged);
	g_hTankRunState.SetBool(false);   // a reloaded plugin starts with no tank run, whatever the cvar kept
	// Follows !survivors, so the web panel can read the team size; setting it
	// does the same as sm_survivors <number>.
	g_hSurvivors = CreateConVar("l4d2_extras_survivors", "0", "Survivor team size kept filled with bots: 0 = not set (the normal 4).", _, true, 0.0, true, 31.0);
	g_hSurvivors.SetInt(0);
	g_hSurvivors.AddChangeHook(OnSurvivorsChanged);

	g_hNextCampaign = CreateConVar("l4d2_extras_nextcampaign", "1", "After a co-op finale is won: 1 = load the next official campaign, 0 = do nothing.", _, true, 0.0, true, 1.0);
	g_hNextCampaignDelay = CreateConVar("l4d2_extras_nextcampaign_delay", "15", "Seconds between winning a finale and loading the next campaign.", _, true, 3.0, true, 120.0);
	HookEvent("finale_win", Event_FinaleWin, EventHookMode_PostNoCopy);
	HookEvent("round_start", Event_RoundStart, EventHookMode_PostNoCopy);
	HookEvent("player_left_start_area", Event_LeftStartArea, EventHookMode_PostNoCopy);

	if (g_hDifficulty != null)
	{
		g_hDifficulty.AddChangeHook(OnDifficultyChanged);
	}

	RegAdminCmd("sm_extras", Command_Extras, ADMFLAG_GENERIC, "Menu with the extra admin commands.");
	RegAdminCmd("sm_difficulty", Command_Difficulty, ADMFLAG_CONVARS, "sm_difficulty [easy|normal|advanced|expert]");
	RegAdminCmd("sm_mode", Command_Mode, ADMFLAG_CHANGEMAP, "sm_mode [coop|realism|mutationN] - set the game mode and reload the map.");
	RegAdminCmd("sm_restartmap", Command_RestartMap, ADMFLAG_CHANGEMAP, "Restart the current chapter.");
	RegAdminCmd("sm_tank", Command_Tank, ADMFLAG_CHEATS, "Spawn a tank.");
	RegAdminCmd("sm_witch", Command_Witch, ADMFLAG_CHEATS, "Spawn a witch.");
	RegAdminCmd("sm_horde", Command_Horde, ADMFLAG_CHEATS, "Call a horde.");
	RegAdminCmd("sm_tankrun", Command_TankRun, ADMFLAG_CHEATS, "sm_tankrun [on|off] - keep tanks coming, map after map, until turned off.");
	LoadTranslations("common.phrases");   // the "no matching client" replies
	HookEvent("player_spawn", Event_PlayerSpawn);
	RegAdminCmd("sm_give", Command_Give, ADMFLAG_CHEATS, "sm_give <item> [who] - give a weapon or item.");
	RegAdminCmd("sm_kit", Command_Kit, ADMFLAG_CHEATS, "sm_kit [who] - give a first aid kit.");
	RegAdminCmd("sm_heal", Command_Heal, ADMFLAG_CHEATS, "sm_heal [who] - restore full health.");
	RegAdminCmd("sm_laser", Command_Laser, ADMFLAG_CHEATS, "sm_laser [who] - put a laser sight on the weapon in hand.");
	RegAdminCmd("sm_ammo", Command_Ammo, ADMFLAG_CHEATS, "sm_ammo [who] - refill ammo.");
	RegAdminCmd("sm_respawn", Command_Respawn, ADMFLAG_CHEATS, "sm_respawn [who] - bring a dead survivor back.");
	RegAdminCmd("sm_roster", Command_Roster, ADMFLAG_GENERIC, "List the survivors with their health (read by the web panel).");
	RegAdminCmd("sm_survivors", Command_Survivors, ADMFLAG_CHEATS, "sm_survivors <number> - survivor team size; bots fill the empty seats.");
	RegAdminCmd("sm_reverseff", Command_ReverseFF, ADMFLAG_CONVARS, "sm_reverseff [on|off] - friendly fire hurts the shooter instead of the teammate.");
}

public void OnMapStart()
{
	g_bConfigsRunning = true;      // server.cfg is about to run
}

public void OnConfigsExecuted()
{
	if (g_hDifficulty != null && g_sWantedDifficulty[0] != '\0')
	{
		g_hDifficulty.SetString(g_sWantedDifficulty);
	}
	// MultiSlots' own config has just reset the team to 4; it fills the team
	// itself when the round starts, so only the numbers go back here.
	ApplyTeamSize();
	ConVar reverse = FindConVar("reverseff_enabled");
	if (reverse != null && g_iWantedReverseFF != -1)
	{
		reverse.SetBool(g_iWantedReverseFF == 1);
	}
	g_bConfigsRunning = false;
}

void OnDifficultyChanged(ConVar convar, const char[] oldValue, const char[] newValue)
{
	if (!g_bConfigsRunning)
	{
		strcopy(g_sWantedDifficulty, sizeof(g_sWantedDifficulty), newValue);
	}
}

public void OnMapEnd()
{
	g_bConfigsRunning = true;
	// The tank run stays on (g_bTankRun), but its timer was created with
	// TIMER_FLAG_NO_MAPCHANGE and is already gone; it starts again once the
	// survivors leave the safe room on the next map.
	g_hTankRunTimer = null;
	g_bLeftStart = false;
	g_hTeamTimer = null;           // same: gone with the map
}

// ------------------------------------------------------------------ helpers

/** Runs a cheat-protected command as a client without turning sv_cheats on. */
void CheatCommand(int client, const char[] command, const char[] args)
{
	int flags = GetCommandFlags(command);
	if (flags == INVALID_FCVAR_FLAGS)
	{
		return;
	}
	SetCommandFlags(command, flags & ~FCVAR_CHEAT);
	FakeClientCommand(client, "%s %s", command, args);
	SetCommandFlags(command, flags);
}

/** Someone to run spawn commands as: a human if there is one, else anyone. */
int AnyClient()
{
	int fallback = 0;
	for (int i = 1; i <= MaxClients; i++)
	{
		if (!IsClientInGame(i))
		{
			continue;
		}
		if (!IsFakeClient(i))
		{
			return i;
		}
		if (fallback == 0)
		{
			fallback = i;
		}
	}
	return fallback;
}

int CountTanks()
{
	int count = 0;
	for (int i = 1; i <= MaxClients; i++)
	{
		if (IsClientInGame(i) && GetClientTeam(i) == TEAM_INFECTED && IsPlayerAlive(i)
			&& GetEntProp(i, Prop_Send, "m_zombieClass") == ZOMBIECLASS_TANK)
		{
			count++;
		}
	}
	return count;
}

bool Spawn(int client, const char[] what)
{
	int target = (client > 0 && IsClientInGame(client)) ? client : AnyClient();
	if (target == 0)
	{
		return false;
	}
	CheatCommand(target, "z_spawn_old", what);
	return true;
}

void ReloadMap(const char[] reason)
{
	char map[64];
	GetCurrentMap(map, sizeof(map));
	ForceChangeLevel(map, reason);
}

// ---------------------------------------------------------------- difficulty

void SetDifficulty(int client, int index)
{
	if (g_hDifficulty == null)
	{
		ReplyToCommand(client, "[Admin] This game has no difficulty setting.");
		return;
	}
	g_hDifficulty.SetString(g_sDifficulty[index][0]);
	PrintToChatAll("%sDifficulty is now %s.", TAG, g_sDifficulty[index][1]);
}

Action Command_Difficulty(int client, int args)
{
	if (args < 1)
	{
		if (client == 0)
		{
			ReplyToCommand(client, "Usage: sm_difficulty <easy|normal|advanced|expert>");
			return Plugin_Handled;
		}
		Menu menu = new Menu(Menu_Difficulty);
		menu.SetTitle("Difficulty");
		for (int i = 0; i < sizeof(g_sDifficulty); i++)
		{
			char info[4];
			IntToString(i, info, sizeof(info));
			menu.AddItem(info, g_sDifficulty[i][1]);
		}
		menu.Display(client, MENU_TIME_FOREVER);
		return Plugin_Handled;
	}

	char arg[32];
	GetCmdArg(1, arg, sizeof(arg));
	for (int i = 0; i < sizeof(g_sDifficulty); i++)
	{
		if (StrEqual(arg, g_sDifficulty[i][0], false) || StrEqual(arg, g_sDifficulty[i][1], false))
		{
			SetDifficulty(client, i);
			return Plugin_Handled;
		}
	}
	ReplyToCommand(client, "[Admin] Unknown difficulty. Use easy, normal, advanced or expert.");
	return Plugin_Handled;
}

int Menu_Difficulty(Menu menu, MenuAction action, int client, int item)
{
	if (action == MenuAction_Select)
	{
		char info[4];
		menu.GetItem(item, info, sizeof(info));
		SetDifficulty(client, StringToInt(info));
	}
	else if (action == MenuAction_End)
	{
		delete menu;
	}
	return 0;
}

// ---------------------------------------------------------------- game mode

void SetMode(int client, const char[] mode, const char[] label)
{
	if (g_hGameMode == null)
	{
		ReplyToCommand(client, "[Admin] This game has no game mode setting.");
		return;
	}
	g_hGameMode.SetString(mode);
	PrintToChatAll("%sGame mode: %s. Reloading the map in 5 seconds...", TAG, label);
	CreateTimer(5.0, Timer_ReloadForMode, _, TIMER_FLAG_NO_MAPCHANGE);
}

Action Timer_ReloadForMode(Handle timer)
{
	ReloadMap("game mode changed");
	return Plugin_Stop;
}

Action Command_Mode(int client, int args)
{
	if (args < 1)
	{
		if (client == 0)
		{
			ReplyToCommand(client, "Usage: sm_mode <coop|realism|mutationN|communityN>");
			return Plugin_Handled;
		}
		Menu menu = new Menu(Menu_Mode);
		menu.SetTitle("Game mode (reloads the map)");
		for (int i = 0; i < sizeof(g_sModes); i++)
		{
			char info[4];
			IntToString(i, info, sizeof(info));
			menu.AddItem(info, g_sModes[i][1]);
		}
		menu.Display(client, MENU_TIME_FOREVER);
		return Plugin_Handled;
	}

	char arg[32];
	GetCmdArg(1, arg, sizeof(arg));
	for (int i = 0; i < sizeof(g_sModes); i++)
	{
		if (StrEqual(arg, g_sModes[i][0], false))
		{
			SetMode(client, g_sModes[i][0], g_sModes[i][1]);
			return Plugin_Handled;
		}
	}
	// Not in the list: accept other mutations by number (mutation12,
	// community3), and nothing else.
	int prefix = 0;
	if (strncmp(arg, "mutation", 8, false) == 0)
	{
		prefix = 8;
	}
	else if (strncmp(arg, "community", 9, false) == 0)
	{
		prefix = 9;
	}
	int digits = strlen(arg) - prefix;
	if (prefix > 0 && digits >= 1 && digits <= 2 && IsCharNumeric(arg[prefix])
		&& (digits == 1 || IsCharNumeric(arg[prefix + 1])) && arg[prefix] != '0')
	{
		for (int i = 0; i < prefix; i++)
		{
			arg[i] = CharToLower(arg[i]);
		}
		SetMode(client, arg, arg);
		return Plugin_Handled;
	}
	ReplyToCommand(client, "[Admin] Unknown mode. Use coop, realism, or a mutation such as mutation4.");
	return Plugin_Handled;
}

int Menu_Mode(Menu menu, MenuAction action, int client, int item)
{
	if (action == MenuAction_Select)
	{
		char info[4];
		menu.GetItem(item, info, sizeof(info));
		int index = StringToInt(info);
		SetMode(client, g_sModes[index][0], g_sModes[index][1]);
	}
	else if (action == MenuAction_End)
	{
		delete menu;
	}
	return 0;
}

Action Command_RestartMap(int client, int args)
{
	PrintToChatAll("%sRestarting the chapter...", TAG);
	CreateTimer(3.0, Timer_Restart, _, TIMER_FLAG_NO_MAPCHANGE);
	return Plugin_Handled;
}

Action Timer_Restart(Handle timer)
{
	ReloadMap("restarted by an admin");
	return Plugin_Stop;
}

// ---------------------------------------------------------- reverse friendly fire

Action Command_ReverseFF(int client, int args)
{
	// Looked up each time: the plugin that owns it may load after this one.
	ConVar reverse = FindConVar("reverseff_enabled");
	if (reverse == null)
	{
		ReplyToCommand(client, "[Admin] Reverse friendly fire isn't installed here: it comes with the mods for more than 4 player slots.");
		return Plugin_Handled;
	}
	bool on = !reverse.BoolValue;
	if (args >= 1)
	{
		char arg[8];
		GetCmdArg(1, arg, sizeof(arg));
		if (StrEqual(arg, "on", false) || StrEqual(arg, "1"))
		{
			on = true;
		}
		else if (StrEqual(arg, "off", false) || StrEqual(arg, "0"))
		{
			on = false;
		}
		else
		{
			ReplyToCommand(client, "[Admin] Usage: sm_reverseff [on|off]");
			return Plugin_Handled;
		}
	}
	g_iWantedReverseFF = on ? 1 : 0;
	reverse.SetBool(on);
	if (on)
	{
		PrintToChatAll("%sReverse friendly fire ON: shoot a teammate and you take the damage.", TAG);
	}
	else
	{
		PrintToChatAll("%sReverse friendly fire OFF.", TAG);
	}
	return Plugin_Handled;
}

// ------------------------------------------------------------ next campaign

/** Where in g_sCampaigns to go after `map`: the campaign after its own, or the first one. */
int NextCampaign(const char[] map)
{
	// Official chapters are named c<campaign>m<chapter>_<place>.
	int number = 0;
	int i = 1;
	if (map[0] == 'c' || map[0] == 'C')
	{
		while (IsCharNumeric(map[i]) && i <= 2)
		{
			number = number * 10 + (map[i] - '0');
			i++;
		}
	}
	if (number < 1 || number > sizeof(g_sCampaigns) || (map[i] != 'm' && map[i] != 'M'))
	{
		return 0;                  // a custom campaign: start the official ones over
	}
	return number % sizeof(g_sCampaigns);
}

void Event_FinaleWin(Event event, const char[] name, bool dontBroadcast)
{
	if (!g_hNextCampaign.BoolValue || g_hGameMode == null)
	{
		return;
	}
	// Co-op and its mutations only: versus, scavenge and survival end their own
	// way. A mutation's name doesn't say which it is (mutation12 is a versus),
	// so Left 4 DHooks is asked; without it, only the plainly named modes count.
	if (GetFeatureStatus(FeatureType_Native, "L4D_GetGameModeType") == FeatureStatus_Available)
	{
		if (L4D_GetGameModeType() != 1)
		{
			return;
		}
	}
	else
	{
		char mode[64];
		g_hGameMode.GetString(mode, sizeof(mode));
		if (!StrEqual(mode, "coop", false) && !StrEqual(mode, "realism", false))
		{
			return;
		}
	}
	char map[64];
	GetCurrentMap(map, sizeof(map));
	int next = NextCampaign(map);
	if (!IsMapValid(g_sCampaigns[next][0]))
	{
		return;
	}
	int delay = g_hNextCampaignDelay.IntValue;
	PrintToChatAll("%sNext campaign: %s, in %d seconds.", TAG, g_sCampaigns[next][1], delay);
	CreateTimer(float(delay), Timer_NextCampaign, next, TIMER_FLAG_NO_MAPCHANGE);
}

Action Timer_NextCampaign(Handle timer, int next)
{
	ForceChangeLevel(g_sCampaigns[next][0], "the finale was won");
	return Plugin_Stop;
}

// ------------------------------------------------------------------- spawns

Action Command_Tank(int client, int args)
{
	if (Spawn(client, "tank auto"))
	{
		PrintToChatAll("%sA tank is coming.", TAG);
	}
	else
	{
		ReplyToCommand(client, "[Admin] Nobody is in the game to spawn it for.");
	}
	return Plugin_Handled;
}

Action Command_Witch(int client, int args)
{
	if (Spawn(client, "witch auto"))
	{
		PrintToChatAll("%sA witch has appeared.", TAG);
	}
	else
	{
		ReplyToCommand(client, "[Admin] Nobody is in the game to spawn it for.");
	}
	return Plugin_Handled;
}

Action Command_Horde(int client, int args)
{
	if (Spawn(client, "mob auto"))
	{
		PrintToChatAll("%sA horde is coming.", TAG);
	}
	else
	{
		ReplyToCommand(client, "[Admin] Nobody is in the game to spawn it for.");
	}
	return Plugin_Handled;
}

// ----------------------------------------------------------------- tank run

void SetTankRun(bool on)
{
	if (on == g_bTankRun)
	{
		g_hTankRunState.SetBool(on);   // already there; just make sure the cvar says so
		return;
	}
	g_bTankRun = on;
	g_hTankRunState.SetBool(on);   // its hook calls back here and stops at the check above
	delete g_hTankRunTimer;
	if (!on)
	{
		PrintToChatAll("%sTank run OFF.", TAG);
		return;
	}
	if (g_bLeftStart)
	{
		StartTankRunTimer();
		Spawn(0, "tank auto");
		PrintToChatAll("%sTank run ON: tanks keep coming, map after map, until it's turned off. (!tankrun off to stop)", TAG);
	}
	else
	{
		PrintToChatAll("%sTank run ON: tanks start once you leave the safe room, and keep coming map after map until it's turned off. (!tankrun off to stop)", TAG);
	}
}

void StartTankRunTimer()
{
	delete g_hTankRunTimer;
	g_hTankRunTimer = CreateTimer(g_hTankRunInterval.FloatValue, Timer_TankRun, _, TIMER_REPEAT | TIMER_FLAG_NO_MAPCHANGE);
}

/** A new map or a restarted round (a wipe): wait for the survivors to leave the safe room again. */
void Event_RoundStart(Event event, const char[] name, bool dontBroadcast)
{
	g_bLeftStart = false;
	delete g_hTankRunTimer;
}

void Event_LeftStartArea(Event event, const char[] name, bool dontBroadcast)
{
	if (g_bLeftStart)
	{
		return;
	}
	g_bLeftStart = true;
	if (g_bTankRun)
	{
		// The first tank comes one interval after leaving, not the moment the door opens.
		StartTankRunTimer();
		PrintToChatAll("%sTank run is on: tanks incoming.", TAG);
	}
}

void OnTankRunStateChanged(ConVar convar, const char[] oldValue, const char[] newValue)
{
	SetTankRun(convar.BoolValue);
}

Action Timer_TankRun(Handle timer)
{
	if (!g_bTankRun)
	{
		g_hTankRunTimer = null;
		return Plugin_Stop;
	}
	if (CountTanks() < g_hTankRunMax.IntValue)
	{
		Spawn(0, "tank auto");
	}
	return Plugin_Continue;
}

Action Command_TankRun(int client, int args)
{
	if (args < 1)
	{
		SetTankRun(!g_bTankRun);
		return Plugin_Handled;
	}
	char arg[8];
	GetCmdArg(1, arg, sizeof(arg));
	if (StrEqual(arg, "on", false) || StrEqual(arg, "1"))
	{
		SetTankRun(true);
	}
	else if (StrEqual(arg, "off", false) || StrEqual(arg, "0"))
	{
		SetTankRun(false);
	}
	else
	{
		ReplyToCommand(client, "[Admin] Usage: sm_tankrun [on|off]");
	}
	return Plugin_Handled;
}

// ------------------------------------------------------------ survivor team

int MaxTeam()
{
	ConVar max = FindConVar("l4d_multislots_max_survivors");
	return max == null ? 0 : max.IntValue;
}

/** Whether the team can be resized: MultiSlots and its bot maker are loaded. */
bool CanResizeTeam()
{
	return FindConVar("l4d_multislots_min_survivors") != null && MaxTeam() >= NORMAL_TEAM
		&& GetFeatureStatus(FeatureType_Native, "CreateSurvivorBot") == FeatureStatus_Available;
}

bool HasIdlePlayer(int bot)
{
	return HasEntProp(bot, Prop_Send, "m_humanSpectatorUserID")
		&& GetEntProp(bot, Prop_Send, "m_humanSpectatorUserID") > 0;
}

int CountSurvivors()
{
	int count = 0;
	for (int i = 1; i <= MaxClients; i++)
	{
		if (IsClientInGame(i) && GetClientTeam(i) == TEAM_SURVIVOR && !IsClientInKickQueue(i))
		{
			count++;
		}
	}
	return count;
}

int AliveSurvivor(int except = 0)
{
	for (int i = 1; i <= MaxClients; i++)
	{
		if (i != except && IsClientInGame(i) && GetClientTeam(i) == TEAM_SURVIVOR && IsPlayerAlive(i))
		{
			return i;
		}
	}
	return 0;
}

/** A bot nobody needs: never one standing in for an idle player, a dead one first. */
int SpareBot()
{
	int alive = 0;
	for (int i = 1; i <= MaxClients; i++)
	{
		if (!IsClientInGame(i) || !IsFakeClient(i) || GetClientTeam(i) != TEAM_SURVIVOR
			|| IsClientInKickQueue(i) || HasIdlePlayer(i))
		{
			continue;
		}
		if (!IsPlayerAlive(i))
		{
			return i;
		}
		alive = i;
	}
	return alive;
}

/** Which of the eight survivors a client is, by its model; -1 when none of them. */
int CharacterOf(int client)
{
	char model[PLATFORM_MAX_PATH];
	GetClientModel(client, model, sizeof(model));
	for (int i = 0; i < sizeof(g_sSurvivorModels); i++)
	{
		if (StrEqual(model, g_sSurvivorModels[i], false))
		{
			return i;
		}
	}
	return -1;
}

bool IsSpareSurvivorBot(int client)
{
	return IsClientInGame(client) && IsFakeClient(client) && GetClientTeam(client) == TEAM_SURVIVOR
		&& !IsClientInKickQueue(client) && !HasIdlePlayer(client);
}

/**
 * The game hands extra bots a character that is already in the team, so a big
 * team ends up with four Coaches. A bot that shares its character with a
 * player, or with a bot before it, becomes the survivor the team has fewest
 * of: look, voice and name.
 */
void GiveOwnCharacter(int bot)
{
	int mine = CharacterOf(bot);
	if (mine < 0)
	{
		return;
	}
	int uses[sizeof(g_sSurvivorModels)];
	bool shared = false;
	for (int i = 1; i <= MaxClients; i++)
	{
		if (i == bot || !IsClientInGame(i) || GetClientTeam(i) != TEAM_SURVIVOR)
		{
			continue;
		}
		int other = CharacterOf(i);
		if (other < 0)
		{
			continue;
		}
		uses[other]++;
		if (other == mine && (i < bot || !IsSpareSurvivorBot(i)))
		{
			shared = true;
		}
	}
	int best = 0;
	for (int i = 1; i < sizeof(uses); i++)
	{
		if (uses[i] < uses[best])
		{
			best = i;
		}
	}
	if (!shared || uses[best] >= uses[mine])
	{
		return;
	}
	if (!IsModelPrecached(g_sSurvivorModels[best]))
	{
		PrecacheModel(g_sSurvivorModels[best], true);
	}
	SetEntProp(bot, Prop_Send, "m_survivorCharacter", best);
	SetEntityModel(bot, g_sSurvivorModels[best]);
	SetClientName(bot, g_sSurvivorNames[best]);
}

void GiveBotsOwnCharacters()
{
	for (int i = 1; i <= MaxClients; i++)
	{
		if (IsSpareSurvivorBot(i))
		{
			GiveOwnCharacter(i);
		}
	}
}

// Bots that MultiSlots makes itself (after a map change) get the same care, but
// only while a bigger team was asked for: a normal game is left alone.
void Event_PlayerSpawn(Event event, const char[] name, bool dontBroadcast)
{
	int userid = event.GetInt("userid");
	int client = GetClientOfUserId(userid);
	if (client != 0 && IsClientInGame(client) && IsFakeClient(client) && WantedTeam() > NORMAL_TEAM)
	{
		CreateTimer(1.0, Timer_OwnCharacter, userid, TIMER_FLAG_NO_MAPCHANGE);
	}
}

Action Timer_OwnCharacter(Handle timer, int userid)
{
	int bot = GetClientOfUserId(userid);
	if (bot != 0 && IsSpareSurvivorBot(bot) && IsPlayerAlive(bot))
	{
		GiveOwnCharacter(bot);
	}
	return Plugin_Stop;
}

bool AddSurvivorBot()
{
	// A new bot is put next to a living survivor; with nobody alive there is
	// nowhere to put it (MultiSlots fills the team when the next round starts).
	if (AliveSurvivor() == 0 || GetClientCount(false) >= MaxClients)
	{
		return false;
	}
	int bot = CreateSurvivorBot();
	if (bot <= 0 || !IsClientInGame(bot))
	{
		return false;
	}
	RequestFrame(Frame_PlaceBot, GetClientUserId(bot));
	return true;
}

void Frame_PlaceBot(int userid)
{
	int bot = GetClientOfUserId(userid);
	if (bot == 0 || !IsClientInGame(bot))
	{
		return;
	}
	int anchor = AliveSurvivor(bot);
	if (anchor != 0)
	{
		float pos[3];
		GetClientAbsOrigin(anchor, pos);
		TeleportEntity(bot, pos, NULL_VECTOR, NULL_VECTOR);
	}
	GiveOwnCharacter(bot);
	CheatCommand(bot, "give", "smg");
	CheatCommand(bot, "give", "first_aid_kit");
}

/** The wanted size, within what this server allows; 0 when there is nothing to do. */
int WantedTeam()
{
	int wanted = g_hSurvivors.IntValue;
	if (wanted <= 0 || !CanResizeTeam())
	{
		return 0;
	}
	int max = MaxTeam();
	return wanted < NORMAL_TEAM ? NORMAL_TEAM : wanted > max ? max : wanted;
}

/** Tells MultiSlots the team size. It only kicks bots above its minimum. */
int ApplyTeamSize()
{
	int wanted = WantedTeam();
	if (wanted == 0)
	{
		return 0;
	}
	FindConVar("l4d_multislots_min_survivors").SetInt(wanted);
	ConVar atStart = FindConVar("l4d_multislots_spawn_survivors_roundstart");
	if (atStart != null && wanted > NORMAL_TEAM)
	{
		atStart.SetBool(true);     // so the bots are back after a map change
	}
	return wanted;
}

/** Applies the size and brings the team to it now, one bot at a time. */
void ResizeTeam()
{
	int wanted = ApplyTeamSize();
	if (wanted == 0)
	{
		return;
	}
	GiveBotsOwnCharacters();       // sort out look-alikes already in the team
	g_iTeamTarget = wanted;
	g_iTeamTries = 0;
	delete g_hTeamTimer;
	g_hTeamTimer = CreateTimer(0.3, Timer_Team, _, TIMER_REPEAT | TIMER_FLAG_NO_MAPCHANGE);
}

Action Timer_Team(Handle timer)
{
	int count = CountSurvivors();
	bool more = false;
	if (++g_iTeamTries <= 2 * MAXPLAYERS)
	{
		if (count < g_iTeamTarget)
		{
			more = AddSurvivorBot();
		}
		else if (count > g_iTeamTarget)
		{
			int bot = SpareBot();
			if (bot != 0)
			{
				KickClient(bot, "The survivor team was made smaller");
				more = true;
			}
		}
	}
	if (!more)
	{
		g_hTeamTimer = null;
		return Plugin_Stop;
	}
	return Plugin_Continue;
}

void OnSurvivorsChanged(ConVar convar, const char[] oldValue, const char[] newValue)
{
	ResizeTeam();
}

Action Command_Survivors(int client, int args)
{
	if (!CanResizeTeam())
	{
		ReplyToCommand(client, "[Admin] The survivor team can't be resized here: that needs the mods for more than 4 player slots (MultiSlots).");
		return Plugin_Handled;
	}
	int max = MaxTeam();
	int wanted = args < 1 ? 0 : GetCmdArgInt(1);
	if (wanted < NORMAL_TEAM || wanted > max)
	{
		ReplyToCommand(client, "[Admin] Usage: sm_survivors <%d-%d>. The team has %d survivors now.", NORMAL_TEAM, max, CountSurvivors());
		return Plugin_Handled;
	}
	if (g_hSurvivors.IntValue == wanted)
	{
		ResizeTeam();              // asked again: top the team up
	}
	else
	{
		g_hSurvivors.SetInt(wanted);   // its hook resizes the team
	}
	ReplyToCommand(client, "[Admin] Survivor team set to %d.", wanted);
	PrintToChatAll("%sSurvivor team: %d. Bots fill the seats nobody plays.", TAG, wanted);
	return Plugin_Handled;
}

// ------------------------------------------------------------------ handouts

/**
 * The survivors a command is for: argument number `arg`, or whoever typed the
 * command when it was left out. Says why when there is nobody. Returns how many.
 */
int FindSurvivors(int client, int args, int arg, bool dead, int[] found)
{
	char pattern[MAX_TARGET_LENGTH];
	if (args >= arg)
	{
		GetCmdArg(arg, pattern, sizeof(pattern));
	}
	else if (client == 0)
	{
		ReplyToCommand(client, "[Admin] Say who: a name, #userid, @all, @bots or @humans.");
		return 0;
	}
	else
	{
		strcopy(pattern, sizeof(pattern), "@me");
	}

	int list[MAXPLAYERS];
	char targetName[MAX_TARGET_LENGTH];
	bool isPhrase;
	int flags = (dead ? COMMAND_FILTER_DEAD : COMMAND_FILTER_ALIVE) | COMMAND_FILTER_NO_IMMUNITY;
	int count = ProcessTargetString(pattern, client, list, MAXPLAYERS, flags, targetName, sizeof(targetName), isPhrase);
	if (count <= 0)
	{
		ReplyToTargetError(client, count);
		return 0;
	}
	int n = 0;
	for (int i = 0; i < count; i++)
	{
		if (GetClientTeam(list[i]) == TEAM_SURVIVOR)
		{
			found[n++] = list[i];
		}
	}
	if (n == 0)
	{
		ReplyToCommand(client, "[Admin] No %s survivor matches that.", dead ? "dead" : "living");
	}
	return n;
}

/** Letters, digits and underscores only: all an item name is ever made of. */
bool IsPlainWord(const char[] text)
{
	if (text[0] == '\0')
	{
		return false;
	}
	for (int i = 0; text[i] != '\0'; i++)
	{
		if (!IsCharAlpha(text[i]) && !IsCharNumeric(text[i]) && text[i] != '_')
		{
			return false;
		}
	}
	return true;
}

/**
 * Weapon upgrades are not items: the game adds them to the gun in hand with a
 * command of their own. They are given under these names all the same.
 */
void GiveItem(int who, const char[] item)
{
	if (StrEqual(item, "laser_sight", false))
	{
		CheatCommand(who, "upgrade_add", "LASER_SIGHT");
	}
	else if (StrEqual(item, "incendiary_ammo", false))
	{
		CheatCommand(who, "upgrade_add", "INCENDIARY_AMMO");
	}
	else if (StrEqual(item, "explosive_ammo", false))
	{
		CheatCommand(who, "upgrade_add", "EXPLOSIVE_AMMO");
	}
	else
	{
		CheatCommand(who, "give", item);
	}
}

/** Gives `item` to the survivors named in argument `arg`; returns how many got it. */
int GiveTo(int client, int args, int arg, const char[] item)
{
	int found[MAXPLAYERS];
	int count = FindSurvivors(client, args, arg, false, found);
	for (int i = 0; i < count; i++)
	{
		GiveItem(found[i], item);
	}
	return count;
}

Action Command_Give(int client, int args)
{
	char item[64];
	if (args >= 1)
	{
		GetCmdArg(1, item, sizeof(item));
	}
	if (!IsPlainWord(item))
	{
		ReplyToCommand(client, "[Admin] Usage: sm_give <item> [who]. Items: rifle_ak47, rifle, autoshotgun, shotgun_spas, sniper_military, smg, pistol_magnum, grenade_launcher, rifle_m60, chainsaw, katana, machete, fireaxe, molotov, pipe_bomb, vomitjar, first_aid_kit, defibrillator, pain_pills, adrenaline, upgradepack_incendiary, upgradepack_explosive, laser_sight, incendiary_ammo, explosive_ammo.");
		return Plugin_Handled;
	}
	int count = GiveTo(client, args, 2, item);
	if (count > 0)
	{
		PrintToChatAll("%s%s given to %d survivor(s).", TAG, item, count);
	}
	return Plugin_Handled;
}

Action Command_Kit(int client, int args)
{
	int count = GiveTo(client, args, 1, "first_aid_kit");
	if (count > 0)
	{
		PrintToChatAll("%sFirst aid kit given to %d survivor(s).", TAG, count);
	}
	return Plugin_Handled;
}

Action Command_Laser(int client, int args)
{
	int count = GiveTo(client, args, 1, "laser_sight");
	if (count > 0)
	{
		PrintToChatAll("%sLaser sight added for %d survivor(s).", TAG, count);
	}
	return Plugin_Handled;
}

Action Command_Ammo(int client, int args)
{
	int count = GiveTo(client, args, 1, "ammo");
	if (count > 0)
	{
		PrintToChatAll("%sAmmo refilled for %d survivor(s).", TAG, count);
	}
	return Plugin_Handled;
}

Action Command_Heal(int client, int args)
{
	int found[MAXPLAYERS];
	int count = FindSurvivors(client, args, 1, false, found);
	for (int i = 0; i < count; i++)
	{
		int who = found[i];
		CheatCommand(who, "give", "health");    // also picks up someone who is down
		// "give health" leaves the limp and the last-life warning behind.
		SetEntPropFloat(who, Prop_Send, "m_healthBuffer", 0.0);
		SetEntProp(who, Prop_Send, "m_currentReviveCount", 0);
		SetEntProp(who, Prop_Send, "m_bIsOnThirdStrike", 0);
		SetEntProp(who, Prop_Send, "m_isGoingToDie", 0);
	}
	if (count > 0)
	{
		PrintToChatAll("%s%d survivor(s) healed.", TAG, count);
	}
	return Plugin_Handled;
}

Action Command_Respawn(int client, int args)
{
	if (GetFeatureStatus(FeatureType_Native, "L4D_RespawnPlayer") != FeatureStatus_Available)
	{
		ReplyToCommand(client, "[Admin] Respawning needs the mods for more than 4 player slots (Left 4 DHooks).");
		return Plugin_Handled;
	}
	int anchor = AliveSurvivor();
	if (anchor == 0)
	{
		ReplyToCommand(client, "[Admin] Nobody is alive to respawn next to.");
		return Plugin_Handled;
	}
	float pos[3];
	GetClientAbsOrigin(anchor, pos);

	int found[MAXPLAYERS];
	int count = FindSurvivors(client, args, 1, true, found);
	for (int i = 0; i < count; i++)
	{
		L4D_RespawnPlayer(found[i]);
		TeleportEntity(found[i], pos, NULL_VECTOR, NULL_VECTOR);
		CheatCommand(found[i], "give", "smg");
	}
	if (count > 0)
	{
		PrintToChatAll("%s%d survivor(s) brought back.", TAG, count);
	}
	return Plugin_Handled;
}

/**
 * One line per survivor, for the web panel:
 *   roster <userid> <bot 0|1> <alive|down|dead> <health> <temporary health> <name>
 * The name comes last because it can hold anything, spaces included.
 */
Action Command_Roster(int client, int args)
{
	ConVar decay = FindConVar("pain_pills_decay_rate");
	for (int i = 1; i <= MaxClients; i++)
	{
		if (!IsClientInGame(i) || GetClientTeam(i) != TEAM_SURVIVOR)
		{
			continue;
		}
		bool alive = IsPlayerAlive(i);
		bool down = alive && GetEntProp(i, Prop_Send, "m_isIncapacitated") != 0;
		int temp = 0;
		if (alive && !down)
		{
			// Temporary health (pills, a revive) drains over time; the game only
			// stores what it was and when.
			float left = GetEntPropFloat(i, Prop_Send, "m_healthBuffer")
				- (GetGameTime() - GetEntPropFloat(i, Prop_Send, "m_healthBufferTime")) * (decay == null ? 0.27 : decay.FloatValue);
			temp = left > 0.0 ? RoundToCeil(left) : 0;
		}
		ReplyToCommand(client, "roster %d %d %s %d %d %N", GetClientUserId(i), IsFakeClient(i) ? 1 : 0,
			!alive ? "dead" : down ? "down" : "alive", alive ? GetClientHealth(i) : 0, temp, i);
	}
	return Plugin_Handled;
}

// --------------------------------------------------------------------- menu

Action Command_Extras(int client, int args)
{
	if (client == 0)
	{
		ReplyToCommand(client, "sm_difficulty, sm_mode, sm_restartmap, sm_tank, sm_witch, sm_horde, sm_tankrun, sm_survivors, sm_reverseff, sm_give, sm_kit, sm_heal, sm_ammo, sm_laser, sm_respawn");
		return Plugin_Handled;
	}
	Menu menu = new Menu(Menu_Extras);
	menu.SetTitle("Admin extras");
	menu.AddItem("sm_difficulty", "Change difficulty");
	menu.AddItem("sm_mode", "Change game mode");
	menu.AddItem("sm_tank", "Spawn a tank");
	menu.AddItem("sm_witch", "Spawn a witch");
	menu.AddItem("sm_horde", "Call a horde");
	menu.AddItem("sm_tankrun", g_bTankRun ? "Tank run: turn OFF" : "Tank run: turn ON (until turned off)");
	ConVar reverse = FindConVar("reverseff_enabled");
	if (reverse != null)
	{
		menu.AddItem("sm_reverseff", reverse.BoolValue ? "Reverse friendly fire: turn OFF" : "Reverse friendly fire: turn ON");
	}
	menu.AddItem("sm_heal @all", "Heal everyone");
	menu.AddItem("sm_kit @all", "First aid kits for everyone");
	menu.AddItem("sm_ammo @all", "Ammo for everyone");
	menu.AddItem("sm_laser @all", "Laser sights for everyone");
	menu.AddItem("sm_respawn @all", "Bring back the dead");
	menu.AddItem("sm_restartmap", "Restart this chapter");
	menu.Display(client, MENU_TIME_FOREVER);
	return Plugin_Handled;
}

int Menu_Extras(Menu menu, MenuAction action, int client, int item)
{
	if (action == MenuAction_Select)
	{
		// Run the real command as the admin, so its own permission check applies.
		char command[32];
		menu.GetItem(item, command, sizeof(command));
		FakeClientCommand(client, "%s", command);
	}
	else if (action == MenuAction_End)
	{
		delete menu;
	}
	return 0;
}
