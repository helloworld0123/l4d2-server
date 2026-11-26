/**
 * Player statistics for the public stats site, kept in SQLite.
 *
 * Every human survivor's kills, teamwork and mistakes are counted per chapter
 * attempt ("round") and written to addons/sourcemod/data/sqlite/l4d2_stats.sq3,
 * which the stats site reads. Players are known by their SteamID64, so a new
 * name keeps the same record. Bots are not counted, and neither is what a bot
 * does while its player is idle.
 *
 * A round is marked "boosted" when the game was made easier while it ran:
 * cheats, infinite ammo, a tank run, double loot, or an admin handing out
 * items, health or extra infected (l4d2_admin_extras says when). The site
 * keeps boosted rounds out of its rankings.
 *
 * Points are worked out by the "scored" view below, from the raw counts, so
 * changing the formula re-scores the whole history. Items passed are counted
 * but earn nothing: two players could pass the same pills back and forth.
 *
 * For players, in chat:
 *   !rank    your place in the rankings
 *   !top     the five best players
 *   !stats   your own totals
 */
#pragma semicolon 1
#pragma newdecls required
#pragma dynamic 16384       // 64 KB of stack for the query buffers

#include <sourcemod>

#define TEAM_SURVIVOR 2
#define TEAM_INFECTED 3
#define TICK 10                  // seconds between playtime ticks
#define FLUSH_EVERY 6            // ticks between saves (one minute)
#define MAX_PENDING 64           // finished rounds kept while the database is unreachable
#define QUERY_MAX 2048
#define STEAMID_MAX 24
#define TAG "\x04[Stats]\x01 "

public Plugin myinfo =
{
	name = "L4D2 stats",
	author = "l4d2-server toolkit",
	description = "Records player statistics for the stats site.",
	version = "1.0",
	url = ""
};

enum
{
	ST_PLAYTIME,
	ST_COMMONS,
	ST_HEADSHOTS,
	ST_SMOKERS,
	ST_BOOMERS,
	ST_HUNTERS,
	ST_SPITTERS,
	ST_JOCKEYS,
	ST_CHARGERS,
	ST_WITCHES,
	ST_TANKS,
	ST_TANK_DAMAGE,
	ST_HEALS,
	ST_REVIVES,
	ST_DEFIBS,
	ST_RESCUES,
	ST_GIVES,
	ST_FF_DAMAGE,
	ST_INCAPS,
	ST_DEATHS,
	ST_SURVIVED,
	ST_COUNT
};

// Column names in round_players, in the order of the enum above.
static const char g_sStatColumns[ST_COUNT][] =
{
	"playtime", "commons", "headshots", "smokers", "boomers", "hunters", "spitters",
	"jockeys", "chargers", "witches", "tanks", "tank_damage", "heals", "revives",
	"defibs", "rescues", "gives", "ff_damage", "incaps", "deaths", "survived"
};

// One statement per line: the stats site's tests read them from this file.
static const char g_sSchema[][] =
{
	"CREATE TABLE IF NOT EXISTS players (steamid TEXT PRIMARY KEY, name TEXT NOT NULL, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL, visits INTEGER NOT NULL DEFAULT 1)",
	"CREATE TABLE IF NOT EXISTS rounds (id INTEGER PRIMARY KEY, map TEXT NOT NULL, mode TEXT NOT NULL, difficulty INTEGER NOT NULL, started INTEGER NOT NULL, left_safe INTEGER, ended INTEGER NOT NULL, result TEXT NOT NULL, boosted INTEGER NOT NULL DEFAULT 0, team_size INTEGER NOT NULL DEFAULT 4, humans INTEGER NOT NULL DEFAULT 0)",
	"CREATE TABLE IF NOT EXISTS round_players (round_id INTEGER NOT NULL, steamid TEXT NOT NULL, playtime INTEGER NOT NULL DEFAULT 0, commons INTEGER NOT NULL DEFAULT 0, headshots INTEGER NOT NULL DEFAULT 0, smokers INTEGER NOT NULL DEFAULT 0, boomers INTEGER NOT NULL DEFAULT 0, hunters INTEGER NOT NULL DEFAULT 0, spitters INTEGER NOT NULL DEFAULT 0, jockeys INTEGER NOT NULL DEFAULT 0, chargers INTEGER NOT NULL DEFAULT 0, witches INTEGER NOT NULL DEFAULT 0, tanks INTEGER NOT NULL DEFAULT 0, tank_damage INTEGER NOT NULL DEFAULT 0, heals INTEGER NOT NULL DEFAULT 0, revives INTEGER NOT NULL DEFAULT 0, defibs INTEGER NOT NULL DEFAULT 0, rescues INTEGER NOT NULL DEFAULT 0, gives INTEGER NOT NULL DEFAULT 0, ff_damage INTEGER NOT NULL DEFAULT 0, incaps INTEGER NOT NULL DEFAULT 0, deaths INTEGER NOT NULL DEFAULT 0, survived INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (round_id, steamid))",
	"CREATE INDEX IF NOT EXISTS round_players_steamid ON round_players (steamid)",
	"CREATE INDEX IF NOT EXISTS rounds_started ON rounds (started)",
	"CREATE TABLE IF NOT EXISTS online (steamid TEXT PRIMARY KEY, name TEXT NOT NULL, since INTEGER NOT NULL)",
	"CREATE TABLE IF NOT EXISTS server_state (k TEXT PRIMARY KEY, v TEXT NOT NULL)",
	"DROP VIEW IF EXISTS scored",
	"CREATE VIEW scored AS SELECT rp.*, r.map, r.mode, r.difficulty, r.started, r.left_safe, r.ended, r.result, r.boosted, r.team_size, rp.smokers + rp.boomers + rp.hunters + rp.spitters + rp.jockeys + rp.chargers AS specials, rp.heals + rp.revives + rp.defibs + rp.rescues AS teamwork, CAST(ROUND((rp.commons + 5 * (rp.smokers + rp.boomers + rp.hunters + rp.spitters + rp.jockeys + rp.chargers) + 15 * rp.witches + 10 * rp.tanks + rp.tank_damage / 50.0 + 10 * rp.heals + 8 * rp.revives + 12 * rp.defibs + 6 * rp.rescues + 25 * rp.survived + 75 * (rp.survived AND r.result = 'won') - rp.ff_damage / 2.0 - 5 * rp.incaps - 20 * rp.deaths) * (CASE r.difficulty WHEN 0 THEN 0.5 WHEN 1 THEN 1.0 WHEN 2 THEN 1.5 ELSE 2.0 END)) AS INTEGER) AS points FROM round_players rp JOIN rounds r ON r.id = rp.round_id"
};

enum struct Round
{
	int id;
	char map[64];
	char mode[32];
	int difficulty;      // the easiest it was set to while the round ran: 0 easy .. 3 expert
	int started;
	int leftSafe;        // when the survivors left the start area (0: not yet)
	int ended;
	char result[16];     // won, cleared, lost, restarted, changed, ended
	bool boosted;
	int team;            // most survivors (humans and bots) seen at once
	StringMap players;   // SteamID64 -> int[ST_COUNT]
}

Database g_hDb;
Round g_Round;
bool g_bRoundOpen;
int g_iLastRoundId;
ArrayList g_hPending;        // finished rounds not saved yet
bool g_bFlushing;
bool g_bFlushAgain;          // a save was asked for while one was running
int g_iTicks;

char g_sSteamId[MAXPLAYERS + 1][STEAMID_MAX];
int g_iSince[MAXPLAYERS + 1];
float g_fLastCommand[MAXPLAYERS + 1];

char g_sColumnList[512];
char g_sUpdateList[1024];

ConVar g_hUrl;
ConVar g_hDifficulty;
ConVar g_hGameMode;
ConVar g_hCheats;
ConVar g_hInfiniteAmmo;
ConVar g_hLoot;

public void OnPluginStart()
{
	g_hUrl = CreateConVar("l4d2_stats_url", "", "Address of the stats site, shown by !rank and !stats.");
	AutoExecConfig(true, "l4d2_stats");
	g_hDifficulty = FindConVar("z_difficulty");
	if (g_hDifficulty != null)
	{
		g_hDifficulty.AddChangeHook(OnDifficultyChanged);
	}
	g_hGameMode = FindConVar("mp_gamemode");
	g_hCheats = FindConVar("sv_cheats");
	g_hInfiniteAmmo = FindConVar("sv_infinite_ammo");
	g_hLoot = FindConVar("director_pain_pill_density");
	ConVar watched[3];
	watched[0] = g_hCheats;
	watched[1] = g_hInfiniteAmmo;
	watched[2] = g_hLoot;
	for (int i = 0; i < sizeof(watched); i++)
	{
		if (watched[i] != null)
		{
			watched[i].AddChangeHook(OnBoostCvarChanged);
		}
	}

	for (int i = 0; i < ST_COUNT; i++)
	{
		Format(g_sColumnList, sizeof(g_sColumnList), "%s, %s", g_sColumnList, g_sStatColumns[i]);
		Format(g_sUpdateList, sizeof(g_sUpdateList), "%s%s%s = excluded.%s", g_sUpdateList,
			i == 0 ? "" : ", ", g_sStatColumns[i], g_sStatColumns[i]);
	}

	g_hPending = new ArrayList(sizeof(Round));
	if (!Connect())
	{
		SetFailState("Could not open the stats database; see the error log.");
	}

	HookEventEx("infected_death", Event_InfectedDeath);
	HookEventEx("player_death", Event_PlayerDeath);
	HookEventEx("player_hurt", Event_PlayerHurt);
	HookEventEx("player_incapacitated", Event_Incapacitated);
	HookEventEx("heal_success", Event_HealSuccess);
	HookEventEx("revive_success", Event_ReviveSuccess);
	HookEventEx("defibrillator_used", Event_DefibUsed);
	HookEventEx("survivor_rescued", Event_SurvivorRescued);
	HookEventEx("witch_killed", Event_WitchKilled);
	HookEventEx("weapon_given", Event_WeaponGiven);
	HookEventEx("player_left_start_area", Event_LeftStartArea, EventHookMode_PostNoCopy);
	HookEventEx("map_transition", Event_MapTransition, EventHookMode_PostNoCopy);
	HookEventEx("finale_win", Event_FinaleWin, EventHookMode_PostNoCopy);
	HookEventEx("mission_lost", Event_MissionLost, EventHookMode_PostNoCopy);
	HookEventEx("round_start", Event_RoundStart, EventHookMode_PostNoCopy);
	HookEventEx("round_end", Event_RoundEnd, EventHookMode_PostNoCopy);

	RegConsoleCmd("sm_rank", Command_Rank, "Your place in the server rankings.");
	RegConsoleCmd("sm_top", Command_Top, "The five best players on this server.");
	RegConsoleCmd("sm_stats", Command_Stats, "Your own totals on this server.");

	CreateTimer(float(TICK), Timer_Tick, _, TIMER_REPEAT);

	// Loaded while a map was already running (a plugin reload).
	for (int i = 1; i <= MaxClients; i++)
	{
		if (IsClientInGame(i))
		{
			Track(i);
		}
	}
	if (IsServerProcessing())
	{
		OpenRound();
	}
}

public void OnPluginEnd()
{
	CloseRound("ended", false);
	FlushNow();
}

bool Connect()
{
	char error[256];
	g_hDb = SQLite_UseDatabase("l4d2_stats", error, sizeof(error));
	if (g_hDb == null)
	{
		LogError("Could not open l4d2_stats: %s", error);
		return false;
	}
	// The stats site reads while we write; wait for it rather than fail.
	SQL_FastQuery(g_hDb, "PRAGMA busy_timeout = 3000");
	// Replacing the points view needs the database to itself, which the site's
	// readers can delay, so it is only replaced when its formula has changed.
	bool viewCurrent = false;
	DBResultSet view = SQL_Query(g_hDb, "SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'scored'");
	if (view != null)
	{
		char current[QUERY_MAX];      // the view is about 1 KB
		if (view.FetchRow())
		{
			view.FetchString(0, current, sizeof(current));
			viewCurrent = StrEqual(current, g_sSchema[sizeof(g_sSchema) - 1]);
		}
		delete view;
	}
	// All or nothing, so the site never finds the points view dropped and not yet back.
	if (!SQL_FastQuery(g_hDb, "BEGIN IMMEDIATE"))
	{
		SQL_GetError(g_hDb, error, sizeof(error));
		LogError("Could not set up the stats tables: %s", error);
		delete g_hDb;
		return false;
	}
	for (int i = 0; i < sizeof(g_sSchema); i++)
	{
		if (viewCurrent && StrContains(g_sSchema[i], " VIEW ") != -1)
		{
			continue;
		}
		if (!SQL_FastQuery(g_hDb, g_sSchema[i]))
		{
			SQL_GetError(g_hDb, error, sizeof(error));
			LogError("Could not set up the stats tables (%d): %s", i, error);
			SQL_FastQuery(g_hDb, "ROLLBACK");
			delete g_hDb;
			return false;
		}
	}
	if (!SQL_FastQuery(g_hDb, "COMMIT"))
	{
		SQL_GetError(g_hDb, error, sizeof(error));
		LogError("Could not set up the stats tables: %s", error);
		delete g_hDb;
		return false;
	}
	// Round ids come from the clock; after the clock steps back, keep counting
	// up from the newest one rather than reusing an old id.
	DBResultSet newest = SQL_Query(g_hDb, "SELECT MAX(id) FROM rounds");
	if (newest != null)
	{
		if (newest.FetchRow() && !newest.IsFieldNull(0))
		{
			g_iLastRoundId = newest.FetchInt(0);
		}
		delete newest;
	}
	return true;
}

// ------------------------------------------------------------------ players

public void OnClientPostAdminCheck(int client)
{
	Track(client);
}

public void OnClientAuthorized(int client, const char[] auth)
{
	if (IsClientInGame(client))
	{
		Track(client);
	}
}

/** Learns a human player's SteamID64 and records the visit. Safe to call twice. */
void Track(int client)
{
	if (IsFakeClient(client) || g_sSteamId[client][0] != '\0')
	{
		return;
	}
	char id[STEAMID_MAX];
	if (!GetClientAuthId(client, AuthId_SteamID64, id, sizeof(id), true) || !IsSteamId(id))
	{
		return;      // not validated by Steam yet (or a LAN game): no record
	}
	strcopy(g_sSteamId[client], STEAMID_MAX, id);
	g_iSince[client] = GetTime();

	char name[MAX_NAME_LENGTH], safe[MAX_NAME_LENGTH * 2 + 1], query[QUERY_MAX];
	GetClientName(client, name, sizeof(name));
	g_hDb.Escape(name, safe, sizeof(safe));
	int now = GetTime();
	// A map change reconnects everyone, so it only counts as a new visit after
	// 15 minutes away (last_seen moves on every minute while they play).
	Format(query, sizeof(query),
		"INSERT INTO players (steamid, name, first_seen, last_seen, visits) VALUES ('%s', '%s', %d, %d, 1) "
		... "ON CONFLICT (steamid) DO UPDATE SET name = excluded.name, "
		... "visits = visits + (last_seen < %d), last_seen = excluded.last_seen",
		id, safe, now, now, now - 900);
	g_hDb.Query(OnQueryDone, query);
}

public void OnClientDisconnect(int client)
{
	if (g_sSteamId[client][0] != '\0' && !IsFakeClient(client) && HumansBesides(client) == 0)
	{
		Flush(client);
	}
	g_sSteamId[client][0] = '\0';
	g_iSince[client] = 0;
	g_fLastCommand[client] = 0.0;
}

int HumansBesides(int client)
{
	int count = 0;
	for (int i = 1; i <= MaxClients; i++)
	{
		if (i != client && IsClientInGame(i) && !IsFakeClient(i))
		{
			count++;
		}
	}
	return count;
}

bool IsSteamId(const char[] id)
{
	int len = strlen(id);
	if (len < 15 || len > 20)
	{
		return false;
	}
	for (int i = 0; i < len; i++)
	{
		if (!IsCharNumeric(id[i]))
		{
			return false;
		}
	}
	return true;
}

/** The client behind a userid if it is a tracked human survivor, else 0. */
int Survivor(int userid)
{
	int client = GetClientOfUserId(userid);
	if (client <= 0 || !IsClientInGame(client) || IsFakeClient(client)
		|| GetClientTeam(client) != TEAM_SURVIVOR || g_sSteamId[client][0] == '\0')
	{
		return 0;
	}
	return client;
}

void Add(int client, int stat, int amount = 1)
{
	if (!g_bRoundOpen || client <= 0 || amount == 0)
	{
		return;
	}
	int stats[ST_COUNT];
	g_Round.players.GetArray(g_sSteamId[client], stats, ST_COUNT);   // a new player stays all zeros
	stats[stat] += amount;
	g_Round.players.SetArray(g_sSteamId[client], stats, ST_COUNT);
}

// ------------------------------------------------------------------- rounds

int DifficultyNow()
{
	char value[32];
	if (g_hDifficulty == null)
	{
		return 1;
	}
	g_hDifficulty.GetString(value, sizeof(value));
	if (StrEqual(value, "Easy", false)) return 0;
	if (StrEqual(value, "Hard", false)) return 2;
	if (StrEqual(value, "Impossible", false)) return 3;
	return 1;
}

bool BoostedNow()
{
	if (g_hCheats != null && g_hCheats.BoolValue) return true;
	if (g_hInfiniteAmmo != null && g_hInfiniteAmmo.BoolValue) return true;
	// The normal density is 6.48; the panel's double loot sets 12.96.
	if (g_hLoot != null && g_hLoot.FloatValue > 7.0) return true;
	ConVar tankRun = FindConVar("l4d2_extras_tankrun");    // l4d2_admin_extras, if loaded
	return tankRun != null && tankRun.BoolValue;
}

int CountSurvivors()
{
	int count = 0;
	for (int i = 1; i <= MaxClients; i++)
	{
		if (IsClientInGame(i) && GetClientTeam(i) == TEAM_SURVIVOR)
		{
			count++;
		}
	}
	return count;
}

void OpenRound()
{
	if (g_bRoundOpen)
	{
		if (g_Round.players.Size > 0)
		{
			return;
		}
		delete g_Round.players;      // nothing happened in it: start it over
		g_bRoundOpen = false;
	}
	int id = GetTime();
	if (id <= g_iLastRoundId)
	{
		id = g_iLastRoundId + 1;
	}
	g_iLastRoundId = id;
	g_Round.id = id;
	GetCurrentMap(g_Round.map, sizeof(g_Round.map));
	g_Round.mode[0] = '\0';
	if (g_hGameMode != null)
	{
		g_hGameMode.GetString(g_Round.mode, sizeof(g_Round.mode));
	}
	g_Round.difficulty = DifficultyNow();
	g_Round.started = GetTime();
	g_Round.leftSafe = 0;
	g_Round.ended = 0;
	g_Round.result[0] = '\0';
	g_Round.boosted = BoostedNow();
	g_Round.team = CountSurvivors();
	g_Round.players = new StringMap();
	g_bRoundOpen = true;
}

/** Ends the round; one in which nobody played is dropped. */
void CloseRound(const char[] result, bool save = true)
{
	if (!g_bRoundOpen)
	{
		return;
	}
	g_bRoundOpen = false;
	if (g_Round.players.Size == 0)
	{
		delete g_Round.players;
		return;
	}
	if (g_Round.result[0] == '\0')
	{
		strcopy(g_Round.result, sizeof(g_Round.result), result);
	}
	g_Round.ended = GetTime();
	if (g_hPending.Length >= MAX_PENDING && !g_bFlushing)
	{
		Round oldest;
		g_hPending.GetArray(0, oldest);
		delete oldest.players;
		g_hPending.Erase(0);
		LogError("The stats database has been unreachable for a long time; the oldest unsaved round was dropped.");
	}
	g_hPending.PushArray(g_Round);
	g_Round.players = null;          // owned by the pending list now
	if (save)
	{
		Flush();
	}
}

/** The survivors still standing made it: to the saferoom, or out on the rescue vehicle. */
void MarkSurvivors(const char[] result)
{
	if (!g_bRoundOpen)
	{
		return;
	}
	strcopy(g_Round.result, sizeof(g_Round.result), result);
	for (int i = 1; i <= MaxClients; i++)
	{
		if (IsClientInGame(i) && !IsFakeClient(i) && GetClientTeam(i) == TEAM_SURVIVOR
			&& IsPlayerAlive(i) && g_sSteamId[i][0] != '\0'
			&& !GetEntProp(i, Prop_Send, "m_isIncapacitated") && !GetEntProp(i, Prop_Send, "m_isHangingFromLedge"))
		{
			int stats[ST_COUNT];
			g_Round.players.GetArray(g_sSteamId[i], stats, ST_COUNT);
			stats[ST_SURVIVED] = 1;
			g_Round.players.SetArray(g_sSteamId[i], stats, ST_COUNT);
		}
	}
}

public void OnMapStart()
{
	OpenRound();
}

public void OnMapEnd()
{
	CloseRound("changed");      // the map was changed before the chapter ended
	Flush();
}

void Event_RoundStart(Event event, const char[] name, bool dontBroadcast)
{
	if (g_bRoundOpen && g_Round.players.Size > 0)
	{
		CloseRound("restarted");
	}
	OpenRound();
}

// The order of round_end and map_transition / finale_win at the end of a chapter
// isn't something to rely on, so round_end only closes the round a moment
// later, giving the event that says how it ended the chance to arrive first.
void Event_RoundEnd(Event event, const char[] name, bool dontBroadcast)
{
	if (g_bRoundOpen)
	{
		CreateTimer(1.0, Timer_RoundEnded, g_Round.id);
	}
}

Action Timer_RoundEnded(Handle timer, int id)
{
	if (g_bRoundOpen && g_Round.id == id)
	{
		CloseRound("ended");
	}
	return Plugin_Stop;
}

void Event_MapTransition(Event event, const char[] name, bool dontBroadcast)
{
	MarkSurvivors("cleared");
	CloseRound("cleared");
}

void Event_FinaleWin(Event event, const char[] name, bool dontBroadcast)
{
	MarkSurvivors("won");
	CloseRound("won");
}

void Event_MissionLost(Event event, const char[] name, bool dontBroadcast)
{
	if (g_bRoundOpen)
	{
		strcopy(g_Round.result, sizeof(g_Round.result), "lost");
	}
	CloseRound("lost");
}

void Event_LeftStartArea(Event event, const char[] name, bool dontBroadcast)
{
	if (g_bRoundOpen && g_Round.leftSafe == 0)
	{
		g_Round.leftSafe = GetTime();
	}
}

void OnDifficultyChanged(ConVar convar, const char[] oldValue, const char[] newValue)
{
	int difficulty = DifficultyNow();
	if (g_bRoundOpen && difficulty < g_Round.difficulty)
	{
		g_Round.difficulty = difficulty;    // switching to expert at the end earns nothing
	}
}

void OnBoostCvarChanged(ConVar convar, const char[] oldValue, const char[] newValue)
{
	if (g_bRoundOpen && BoostedNow())
	{
		g_Round.boosted = true;
	}
}

/** From l4d2_admin_extras: an admin gave survivors something or spawned infected. */
public void L4D2Extras_OnAdminHelp()
{
	if (g_bRoundOpen)
	{
		g_Round.boosted = true;
	}
}

Action Timer_Tick(Handle timer)
{
	if (g_bRoundOpen)
	{
		for (int i = 1; i <= MaxClients; i++)
		{
			if (IsClientInGame(i) && !IsFakeClient(i) && GetClientTeam(i) == TEAM_SURVIVOR && g_sSteamId[i][0] != '\0')
			{
				Add(i, ST_PLAYTIME, TICK);
			}
		}
		int difficulty = DifficultyNow();
		if (difficulty < g_Round.difficulty)
		{
			g_Round.difficulty = difficulty;    // switching to expert at the end earns nothing
		}
		if (BoostedNow())
		{
			g_Round.boosted = true;
		}
		int team = CountSurvivors();
		if (team > g_Round.team)
		{
			g_Round.team = team;
		}
	}
	if (++g_iTicks >= FLUSH_EVERY)
	{
		g_iTicks = 0;
		Flush();
	}
	return Plugin_Continue;
}

// ------------------------------------------------------------------- events

void Event_InfectedDeath(Event event, const char[] name, bool dontBroadcast)
{
	int attacker = Survivor(event.GetInt("attacker"));
	int infected = event.GetInt("infected_id");
	if (attacker && infected > MaxClients && IsValidEntity(infected))
	{
		char cls[32];
		GetEntityClassname(infected, cls, sizeof(cls));
		if (!StrEqual(cls, "infected"))
		{
			return;      // a witch: witch_killed counts those
		}
	}
	if (attacker)
	{
		Add(attacker, ST_COMMONS);
		if (event.GetBool("headshot"))
		{
			Add(attacker, ST_HEADSHOTS);
		}
	}
}

void Event_PlayerDeath(Event event, const char[] name, bool dontBroadcast)
{
	int victim = GetClientOfUserId(event.GetInt("userid"));
	if (victim <= 0 || !IsClientInGame(victim))
	{
		return;      // commons and witches come through their own events
	}
	int team = GetClientTeam(victim);
	if (team == TEAM_INFECTED)
	{
		int attacker = Survivor(event.GetInt("attacker"));
		if (!attacker)
		{
			return;
		}
		switch (GetEntProp(victim, Prop_Send, "m_zombieClass"))
		{
			case 1: Add(attacker, ST_SMOKERS);
			case 2: Add(attacker, ST_BOOMERS);
			case 3: Add(attacker, ST_HUNTERS);
			case 4: Add(attacker, ST_SPITTERS);
			case 5: Add(attacker, ST_JOCKEYS);
			case 6: Add(attacker, ST_CHARGERS);
			case 8: Add(attacker, ST_TANKS);
			default: return;
		}
		if (event.GetBool("headshot"))
		{
			Add(attacker, ST_HEADSHOTS);
		}
	}
	else if (team == TEAM_SURVIVOR && !IsFakeClient(victim) && g_sSteamId[victim][0] != '\0')
	{
		Add(victim, ST_DEATHS);
	}
}

void Event_PlayerHurt(Event event, const char[] name, bool dontBroadcast)
{
	int attacker = Survivor(event.GetInt("attacker"));
	int victim = GetClientOfUserId(event.GetInt("userid"));
	if (!attacker || victim <= 0 || victim == attacker || !IsClientInGame(victim))
	{
		return;
	}
	int damage = event.GetInt("dmg_health");
	int team = GetClientTeam(victim);
	if (team == TEAM_INFECTED && GetEntProp(victim, Prop_Send, "m_zombieClass") == 8)
	{
		if (GetEntProp(victim, Prop_Send, "m_isIncapacitated"))
		{
			return;      // already dying: hits on the death animation don't count
		}
		int left = event.GetInt("health");
		if (left < 0)
		{
			damage += left;      // only what the tank actually had left
		}
		if (damage > 0)
		{
			Add(attacker, ST_TANK_DAMAGE, damage);
		}
	}
	else if (team == TEAM_SURVIVOR && damage > 0)
	{
		Add(attacker, ST_FF_DAMAGE, damage);
	}
}

void Event_Incapacitated(Event event, const char[] name, bool dontBroadcast)
{
	int victim = Survivor(event.GetInt("userid"));
	if (victim)
	{
		Add(victim, ST_INCAPS);
	}
}

void Event_HealSuccess(Event event, const char[] name, bool dontBroadcast)
{
	int healer = Survivor(event.GetInt("userid"));
	if (healer && GetClientOfUserId(event.GetInt("subject")) != healer)
	{
		Add(healer, ST_HEALS);      // healing yourself is not teamwork
	}
}

void Event_ReviveSuccess(Event event, const char[] name, bool dontBroadcast)
{
	int reviver = Survivor(event.GetInt("userid"));
	// Pulling someone up from a ledge isn't counted: hanging costs nothing, so
	// two players could take turns hanging and helping each other up.
	if (reviver && !event.GetBool("ledge_hang") && GetClientOfUserId(event.GetInt("subject")) != reviver)
	{
		Add(reviver, ST_REVIVES);
	}
}

void Event_DefibUsed(Event event, const char[] name, bool dontBroadcast)
{
	int medic = Survivor(event.GetInt("userid"));
	if (medic)
	{
		Add(medic, ST_DEFIBS);
	}
}

void Event_SurvivorRescued(Event event, const char[] name, bool dontBroadcast)
{
	int rescuer = Survivor(event.GetInt("rescuer"));
	if (rescuer)
	{
		Add(rescuer, ST_RESCUES);
	}
}

void Event_WitchKilled(Event event, const char[] name, bool dontBroadcast)
{
	int killer = Survivor(event.GetInt("userid"));
	if (killer)
	{
		Add(killer, ST_WITCHES);
	}
}

void Event_WeaponGiven(Event event, const char[] name, bool dontBroadcast)
{
	int giver = Survivor(event.GetInt("giver"));
	if (giver && GetClientOfUserId(event.GetInt("userid")) != giver)
	{
		Add(giver, ST_GIVES);
	}
}

// ----------------------------------------------------------------- saving

/** Every statement that brings the database up to date, finished rounds first. */
void BuildQueries(ArrayList out, int pending, int leaving = 0)
{
	Round r;
	for (int i = 0; i < pending; i++)
	{
		g_hPending.GetArray(i, r);
		AddRoundQueries(out, r);
	}
	if (g_bRoundOpen && g_Round.players.Size > 0)
	{
		// A copy, so the round in progress keeps no end time or result of its own.
		Round live;
		live = g_Round;
		live.ended = GetTime();
		if (live.result[0] == '\0')
		{
			strcopy(live.result, sizeof(live.result), "playing");
		}
		AddRoundQueries(out, live);
	}
	AddLiveQueries(out, leaving);
}

void AddRoundQueries(ArrayList out, Round r)
{
	char query[QUERY_MAX], map[sizeof(r.map) * 2 + 1], mode[sizeof(r.mode) * 2 + 1], leftSafe[16];
	g_hDb.Escape(r.map, map, sizeof(map));
	g_hDb.Escape(r.mode, mode, sizeof(mode));
	if (r.leftSafe > 0)
	{
		IntToString(r.leftSafe, leftSafe, sizeof(leftSafe));
	}
	else
	{
		strcopy(leftSafe, sizeof(leftSafe), "NULL");
	}
	StringMapSnapshot snap = r.players.Snapshot();
	Format(query, sizeof(query),
		"INSERT INTO rounds (id, map, mode, difficulty, started, left_safe, ended, result, boosted, team_size, humans) "
		... "VALUES (%d, '%s', '%s', %d, %d, %s, %d, '%s', %d, %d, %d) ON CONFLICT (id) DO UPDATE SET "
		... "difficulty = excluded.difficulty, left_safe = excluded.left_safe, ended = excluded.ended, "
		... "result = excluded.result, boosted = excluded.boosted, team_size = excluded.team_size, humans = excluded.humans",
		r.id, map, mode, r.difficulty, r.started, leftSafe, r.ended, r.result, r.boosted ? 1 : 0, r.team, snap.Length);
	out.PushString(query);

	char id[STEAMID_MAX];
	int stats[ST_COUNT];
	for (int i = 0; i < snap.Length; i++)
	{
		snap.GetKey(i, id, sizeof(id));
		r.players.GetArray(id, stats, ST_COUNT);
		// Whole values, not increments: saving the same round again is harmless.
		Format(query, sizeof(query), "INSERT INTO round_players (round_id, steamid%s) VALUES (%d, '%s'", g_sColumnList, r.id, id);
		for (int s = 0; s < ST_COUNT; s++)
		{
			Format(query, sizeof(query), "%s, %d", query, stats[s]);
		}
		Format(query, sizeof(query), "%s) ON CONFLICT (round_id, steamid) DO UPDATE SET %s", query, g_sUpdateList);
		out.PushString(query);
	}
	delete snap;
}

/** Who is playing right now and on which map, for the site's "now playing". */
void AddLiveQueries(ArrayList out, int leaving)
{
	char query[QUERY_MAX], name[MAX_NAME_LENGTH], safe[MAX_NAME_LENGTH * 2 + 1];
	int now = GetTime();
	out.PushString("DELETE FROM online");
	for (int i = 1; i <= MaxClients; i++)
	{
		if (i == leaving || !IsClientInGame(i) || IsFakeClient(i) || g_sSteamId[i][0] == '\0')
		{
			continue;
		}
		GetClientName(i, name, sizeof(name));
		g_hDb.Escape(name, safe, sizeof(safe));
		Format(query, sizeof(query), "INSERT INTO online (steamid, name, since) VALUES ('%s', '%s', %d)",
			g_sSteamId[i], safe, g_iSince[i]);
		out.PushString(query);
		Format(query, sizeof(query),
			"INSERT INTO players (steamid, name, first_seen, last_seen, visits) VALUES ('%s', '%s', %d, %d, 1) "
			... "ON CONFLICT (steamid) DO UPDATE SET name = excluded.name, last_seen = excluded.last_seen",
			g_sSteamId[i], safe, now, now);
		out.PushString(query);
	}
	char map[64], safeMap[129], mode[32], safeMode[65];
	GetCurrentMap(map, sizeof(map));
	g_hDb.Escape(map, safeMap, sizeof(safeMap));
	mode[0] = '\0';
	if (g_hGameMode != null)
	{
		g_hGameMode.GetString(mode, sizeof(mode));
	}
	g_hDb.Escape(mode, safeMode, sizeof(safeMode));
	Format(query, sizeof(query),
		"INSERT INTO server_state (k, v) VALUES ('map', '%s'), ('mode', '%s'), ('difficulty', '%d'), "
		... "('max_players', '%d'), ('updated', '%d') ON CONFLICT (k) DO UPDATE SET v = excluded.v",
		safeMap, safeMode, DifficultyNow(), GetMaxHumanPlayers(), now);
	out.PushString(query);
}

/** Saves in the background. Anything that fails stays queued for the next try. */
void Flush(int leaving = 0)
{
	if (g_hDb == null)
	{
		return;
	}
	if (g_bFlushing)
	{
		g_bFlushAgain = true;    // e.g. the last player leaving: saved once this one is done
		return;
	}
	g_bFlushAgain = false;
	int pending = g_hPending.Length;
	ArrayList queries = new ArrayList(ByteCountToCells(QUERY_MAX));
	BuildQueries(queries, pending, leaving);
	Transaction txn = new Transaction();
	char query[QUERY_MAX];
	for (int i = 0; i < queries.Length; i++)
	{
		queries.GetString(i, query, sizeof(query));
		txn.AddQuery(query);
	}
	delete queries;
	g_bFlushing = true;
	g_hDb.Execute(txn, OnFlushed, OnFlushFailed, pending);
}

void OnFlushed(Database db, any pending, int numQueries, DBResultSet[] results, any[] queryData)
{
	g_bFlushing = false;
	ForgetSaved(pending);
	if (g_bFlushAgain)
	{
		Flush();
	}
}

void OnFlushFailed(Database db, any pending, int numQueries, const char[] error, int failIndex, any[] queryData)
{
	g_bFlushing = false;         // anything still owed, g_bFlushAgain included, goes with the next tick
	LogError("Could not save stats (statement %d): %s. Trying again in a minute.", failIndex, error);
}

/** The first `count` finished rounds are in the database now. */
void ForgetSaved(int count)
{
	Round r;
	for (int i = 0; i < count && g_hPending.Length > 0; i++)
	{
		g_hPending.GetArray(0, r);
		delete r.players;
		g_hPending.Erase(0);
	}
}

/** Saves right away, on the game thread: only when the plugin is unloading. */
void FlushNow()
{
	if (g_hDb == null)
	{
		return;
	}
	int pending = g_hPending.Length;
	ArrayList queries = new ArrayList(ByteCountToCells(QUERY_MAX));
	BuildQueries(queries, pending);
	char query[QUERY_MAX];
	bool ok = true;
	SQL_LockDatabase(g_hDb);
	SQL_FastQuery(g_hDb, "BEGIN");
	for (int i = 0; i < queries.Length && ok; i++)
	{
		queries.GetString(i, query, sizeof(query));
		ok = SQL_FastQuery(g_hDb, query);
	}
	if (ok)
	{
		ok = SQL_FastQuery(g_hDb, "COMMIT");
	}
	if (!ok)
	{
		char error[256];
		SQL_GetError(g_hDb, error, sizeof(error));
		SQL_FastQuery(g_hDb, "ROLLBACK");
		LogError("Could not save stats while unloading: %s", error);
	}
	SQL_UnlockDatabase(g_hDb);
	delete queries;
	if (ok)
	{
		ForgetSaved(pending);
	}
}

void OnQueryDone(Database db, DBResultSet results, const char[] error, any data)
{
	if (results == null)
	{
		LogError("Stats query failed: %s", error);
	}
}

// ----------------------------------------------------------------- commands

/** One command every three seconds per player: each one reads the database. */
bool Ready(int client)
{
	if (client <= 0 || !IsClientInGame(client))
	{
		ReplyToCommand(client, "[Stats] Use this from the game.");
		return false;
	}
	if (g_sSteamId[client][0] == '\0')
	{
		ReplyToCommand(client, "[Stats] Steam hasn't confirmed who you are yet. Try again in a moment.");
		return false;
	}
	float now = GetGameTime();
	if (g_fLastCommand[client] > 0.0 && now - g_fLastCommand[client] < 3.0)
	{
		return false;
	}
	g_fLastCommand[client] = now;
	return true;
}

void SiteHint(int client)
{
	char url[192];
	g_hUrl.GetString(url, sizeof(url));
	if (url[0] != '\0')
	{
		PrintToChat(client, "%sFull stats: %s", TAG, url);
	}
}

Action Command_Rank(int client, int args)
{
	if (!Ready(client))
	{
		return Plugin_Handled;
	}
	char query[QUERY_MAX];
	Format(query, sizeof(query),
		"WITH t AS (SELECT steamid, SUM(points) AS p, SUM(playtime) AS pt FROM scored WHERE boosted = 0 GROUP BY steamid HAVING SUM(playtime) > 0) "
		... "SELECT (SELECT COUNT(*) FROM t WHERE t.p > me.p) + 1, me.p, (SELECT COUNT(*) FROM t), me.pt FROM t AS me WHERE me.steamid = '%s'",
		g_sSteamId[client]);
	g_hDb.Query(OnRank, query, GetClientUserId(client));
	return Plugin_Handled;
}

void OnRank(Database db, DBResultSet results, const char[] error, any userid)
{
	int client = GetClientOfUserId(userid);
	if (client <= 0)
	{
		return;
	}
	if (results == null)
	{
		LogError("!rank failed: %s", error);
		PrintToChat(client, "%sCouldn't read the rankings just now.", TAG);
		return;
	}
	if (!results.FetchRow())
	{
		PrintToChat(client, "%sYou're not ranked yet: finish a chapter first (games with cheats or admin help don't count).", TAG);
		return;
	}
	PrintToChat(client, "%sYou're \x05#%d\x01 of %d with \x05%d\x01 points in %.1f hours.", TAG,
		results.FetchInt(0), results.FetchInt(2), results.FetchInt(1), results.FetchInt(3) / 3600.0);
	SiteHint(client);
}

Action Command_Top(int client, int args)
{
	if (!Ready(client))
	{
		return Plugin_Handled;
	}
	g_hDb.Query(OnTop,
		"SELECT p.name, SUM(s.points) AS pts FROM scored AS s JOIN players AS p ON p.steamid = s.steamid "
		... "WHERE s.boosted = 0 GROUP BY s.steamid HAVING SUM(s.playtime) > 0 ORDER BY pts DESC LIMIT 5",
		GetClientUserId(client));
	return Plugin_Handled;
}

void OnTop(Database db, DBResultSet results, const char[] error, any userid)
{
	int client = GetClientOfUserId(userid);
	if (client <= 0)
	{
		return;
	}
	if (results == null)
	{
		LogError("!top failed: %s", error);
		PrintToChat(client, "%sCouldn't read the rankings just now.", TAG);
		return;
	}
	if (results.RowCount == 0)
	{
		PrintToChat(client, "%sNobody is ranked yet.", TAG);
		return;
	}
	PrintToChat(client, "%sTop players:", TAG);
	char name[MAX_NAME_LENGTH];
	int place = 0;
	while (results.FetchRow())
	{
		results.FetchString(0, name, sizeof(name));
		for (int c = 0; name[c] != '\0'; c++)
		{
			if (name[c] > 0 && name[c] < 32)
			{
				name[c] = ' ';      // chat colour codes: nobody paints their own entry
			}
		}
		PrintToChat(client, "  %d. %s - \x05%d\x01", ++place, name, results.FetchInt(1));
	}
	SiteHint(client);
}

Action Command_Stats(int client, int args)
{
	if (!Ready(client))
	{
		return Plugin_Handled;
	}
	char query[QUERY_MAX];
	Format(query, sizeof(query),
		"SELECT SUM(commons), SUM(specials), SUM(tanks), SUM(witches), SUM(teamwork), SUM(deaths), SUM(playtime), "
		... "SUM(survived) FROM scored WHERE steamid = '%s'", g_sSteamId[client]);
	g_hDb.Query(OnStats, query, GetClientUserId(client));
	return Plugin_Handled;
}

void OnStats(Database db, DBResultSet results, const char[] error, any userid)
{
	int client = GetClientOfUserId(userid);
	if (client <= 0)
	{
		return;
	}
	if (results == null || !results.FetchRow())
	{
		LogError("!stats failed: %s", error);
		PrintToChat(client, "%sCouldn't read your stats just now.", TAG);
		return;
	}
	PrintToChat(client, "%s%.1f h played, %d chapters survived.", TAG, results.FetchInt(6) / 3600.0, results.FetchInt(7));
	PrintToChat(client, "  Kills: %d common, %d special, %d tanks, %d witches.",
		results.FetchInt(0), results.FetchInt(1), results.FetchInt(2), results.FetchInt(3));
	PrintToChat(client, "  Heals, revives and rescues: %d. Deaths: %d.", results.FetchInt(4), results.FetchInt(5));
	SiteHint(client);
}
