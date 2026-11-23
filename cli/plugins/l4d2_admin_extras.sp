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
 *   !tankrun [on|off]       keep tanks coming until the map ends
 */
#pragma semicolon 1
#pragma newdecls required

#include <sourcemod>
#include <sdktools>

#define TEAM_INFECTED 3
#define ZOMBIECLASS_TANK 8
#define TAG "\x04[Admin]\x01 "

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

ConVar g_hDifficulty;
ConVar g_hGameMode;
ConVar g_hTankRunInterval;
ConVar g_hTankRunMax;
ConVar g_hTankRunState;
Handle g_hTankRunTimer;
bool g_bTankRun;

// server.cfg sets z_difficulty and the game runs it again on every map load,
// which would undo a difficulty picked in game. So the last difficulty chosen
// while a map was running (here, by a vote, or from the web panel) is kept and
// put back once the configs have run. A server restart forgets it.
char g_sWantedDifficulty[32];
bool g_bConfigsRunning = true;

public void OnPluginStart()
{
	g_hDifficulty = FindConVar("z_difficulty");
	g_hGameMode = FindConVar("mp_gamemode");
	g_hTankRunInterval = CreateConVar("l4d2_extras_tankrun_interval", "25", "Tank run: seconds between tank spawns.", _, true, 5.0, true, 300.0);
	g_hTankRunMax = CreateConVar("l4d2_extras_tankrun_max", "2", "Tank run: most tanks alive at once.", _, true, 1.0, true, 8.0);
	// Follows the tank run, so the web panel can read whether one is on; setting
	// it does the same as sm_tankrun on/off.
	g_hTankRunState = CreateConVar("l4d2_extras_tankrun", "0", "Tank run on this map: 1 = on, 0 = off.", _, true, 0.0, true, 1.0);
	g_hTankRunState.AddChangeHook(OnTankRunStateChanged);
	g_hTankRunState.SetBool(false);   // a reloaded plugin starts with no tank run, whatever the cvar kept

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
	RegAdminCmd("sm_tankrun", Command_TankRun, ADMFLAG_CHEATS, "sm_tankrun [on|off] - keep tanks coming until the map ends.");
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
	// A tank run is for one map. The timer was created with
	// TIMER_FLAG_NO_MAPCHANGE, so it is already gone; just forget it.
	g_bTankRun = false;
	g_hTankRunTimer = null;
	g_hTankRunState.SetBool(false);
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
	if (on)
	{
		g_hTankRunTimer = CreateTimer(g_hTankRunInterval.FloatValue, Timer_TankRun, _, TIMER_REPEAT | TIMER_FLAG_NO_MAPCHANGE);
		Spawn(0, "tank auto");
		PrintToChatAll("%sTank run ON: tanks keep coming until this map ends. (!tankrun off to stop)", TAG);
	}
	else
	{
		PrintToChatAll("%sTank run OFF.", TAG);
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

// --------------------------------------------------------------------- menu

Action Command_Extras(int client, int args)
{
	if (client == 0)
	{
		ReplyToCommand(client, "sm_difficulty, sm_mode, sm_restartmap, sm_tank, sm_witch, sm_horde, sm_tankrun");
		return Plugin_Handled;
	}
	Menu menu = new Menu(Menu_Extras);
	menu.SetTitle("Admin extras");
	menu.AddItem("sm_difficulty", "Change difficulty");
	menu.AddItem("sm_mode", "Change game mode");
	menu.AddItem("sm_tank", "Spawn a tank");
	menu.AddItem("sm_witch", "Spawn a witch");
	menu.AddItem("sm_horde", "Call a horde");
	menu.AddItem("sm_tankrun", g_bTankRun ? "Tank run: turn OFF" : "Tank run: turn ON (this map)");
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
