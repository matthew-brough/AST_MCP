global using System.Linq;
using CitizenFX.Core;

namespace Sample.Server;

// Enhanced (.NET 10) style: file-scoped namespace, records, primary constructors.
public record PlayerInfo(string Name, int Id);

public sealed record class Vehicle(string Model)
{
    public required string Plate { get; init; }
}

public readonly record struct Coord(float X, float Y, float Z);

/// <summary>Server script with a primary constructor.</summary>
public partial class ServerMain(ILogger logger) : BaseScript
{
    private readonly int[] _ids = [1, 2, 3];

    public void Log(string message) => logger.Info(message);

    public static ServerMain operator +(ServerMain a, ServerMain b) => a;

    public string this[int index] => index.ToString();

    ~ServerMain() { }
}

public static class StringExtensions
{
    extension(string value)
    {
        public bool IsBlank => string.IsNullOrWhiteSpace(value);
    }

    public static string Shout(this string s) => s.ToUpper();
}
