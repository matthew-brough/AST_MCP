using System;
using System.Collections.Generic;
using System.Threading.Tasks;
using CitizenFX.Core;
using static CitizenFX.Core.Native.API;
using Json = Newtonsoft.Json.JsonConvert;

namespace Sample.Client
{
    /// <summary>
    /// Legacy (mono) style client script.
    /// </summary>
    public class ClientMain : BaseScript
    {
        private readonly Dictionary<string, int> _counts = new Dictionary<string, int>();

        public const int MaxPlayers = 32;

        public int Health { get; set; }

        public ClientMain()
        {
            EventHandlers["onClientResourceStart"] += new Action<string>(OnClientResourceStart);
            EventHandlers["sample:heartbeat"] += new Action<string>((message) => { });
            Exports.Add("getHealth", new Func<int>(() => Health));
            RegisterCommand("heal", new Action<int, List<object>, string>((source, args, raw) =>
            {
                Health = 100;
            }), false);
            Query("SELECT 1", null, new Action<object>((rows) => { }));
        }

        /// <summary>Fires when a resource starts.</summary>
        private void OnClientResourceStart(string resourceName)
        {
            if (GetCurrentResourceName() != resourceName) return;
        }

        [Tick]
        public async Task OnTick()
        {
            await Delay(0);
        }

        [EventHandler("sample:greet")]
        private void OnGreet([FromSource] Player source, string message) { }

        public event EventHandler Changed;

        public delegate void Callback(int value);

        private struct Point
        {
            public int X;
            public int Y;
        }
    }

    public interface IGreeter
    {
        string Greet(string name);
    }

    public enum Weather
    {
        Clear,
        Rain,
    }
}
