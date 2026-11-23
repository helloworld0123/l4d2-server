#pragma semicolon 1
#pragma newdecls required

#include <sourcemod>
#include <sdktools>

public Plugin myinfo =
{
	name = "L4D2 precache all survivors",
	author = "l4d2-server toolkit",
	description = "Loads all eight survivor models on every map, so a bot given an L4D1 character can't crash the server.",
	version = "1.0",
	url = ""
};

static const char g_sModels[][] =
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

public void OnMapStart()
{
	for (int i = 0; i < sizeof(g_sModels); i++)
	{
		if (!IsModelPrecached(g_sModels[i]))
		{
			PrecacheModel(g_sModels[i], true);
		}
	}
}
