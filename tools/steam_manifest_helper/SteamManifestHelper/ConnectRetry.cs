namespace SteamManifestHelper;

/// <summary>How often the gateway tries to open the CM connection BEFORE its single
/// LogOn: 3 attempts, 5 s and then 15 s apart. Only the connect is retried. A logon
/// is never retried, so this adds no logons (#361).</summary>
internal static class ConnectRetry
{
    internal static readonly TimeSpan[] Waits = [TimeSpan.FromSeconds(5), TimeSpan.FromSeconds(15)];

    /// <summary>Runs `connect` until it succeeds. A network fault (SessionLostException
    /// or TimeoutException) is retried after the next wait; the last one is rethrown.
    /// Anything else, a cancellation included, propagates at once.</summary>
    internal static async Task RunAsync(Func<Task> connect, Func<TimeSpan, Task> delay)
    {
        for (var attempt = 0; ; attempt++)
        {
            try
            {
                await connect();
                return;
            }
            catch (Exception e) when ((e is SessionLostException or TimeoutException) && attempt < Waits.Length)
            {
                await delay(Waits[attempt]);
            }
        }
    }
}
