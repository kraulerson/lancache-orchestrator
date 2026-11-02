namespace SteamManifestHelper;

/// <summary>Re-sends a dropped or failed request (#369). Steam silently drops about
/// 1% of ClientGetDepotDecryptionKey requests: a dropped one gets no answer at all
/// (answered ones take under 250 ms), and re-sending it is answered at once. SteamKit2's
/// job timeout (~10 s) already spaces the attempts, so there is no delay here.</summary>
internal static class RequestRetry
{
    /// <summary>Runs `attempt` up to `attempts` times. Only a SteamRequestException (no
    /// answer or a failed job) is retried; the last one is rethrown. SessionLostException belongs to
    /// FetchRunner's single reconnect, and every other exception, a cancellation
    /// included, propagates at once. `onRetry` gets the 1-based number of the attempt
    /// that just failed.</summary>
    internal static async Task<T> RunAsync<T>(Func<Task<T>> attempt, int attempts, Action<int, Exception>? onRetry = null)
    {
        for (var n = 1; ; n++)
        {
            try
            {
                return await attempt();
            }
            catch (SteamRequestException e) when (n < attempts)
            {
                onRetry?.Invoke(n, e);
            }
        }
    }
}
