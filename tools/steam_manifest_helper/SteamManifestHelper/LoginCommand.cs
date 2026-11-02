using System.Text;
using SteamKit2;
using SteamKit2.Authentication;

namespace SteamManifestHelper;

/// <summary>One-time interactive login: password, then Steam Guard approval on the
/// phone. Needed when the imported token is rejected or expires, because nothing
/// else can renew the session once DepotDownloader is gone.</summary>
public static class LoginCommand
{
    public static async Task<int> RunAsync(string username, string sessionDir, TextWriter log, CancellationToken ct)
    {
        log.Write($"Steam password for {username}: ");
        var password = ReadSecret();
        var client = new SteamClient();
        var manager = new CallbackManager(client);
        var connected = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        manager.Subscribe<SteamClient.ConnectedCallback>(_ => connected.TrySetResult());
        manager.Subscribe<SteamClient.DisconnectedCallback>(_ => connected.TrySetException(new SessionLostException("disconnected before login")));
        using var pumpStop = new CancellationTokenSource();
        var pump = Task.Run(() =>
        {
            while (!pumpStop.IsCancellationRequested)
            {
                manager.RunWaitCallbacks(TimeSpan.FromMilliseconds(250));
            }
        });
        try
        {
            client.Connect();
            await connected.Task.WaitAsync(TimeSpan.FromSeconds(30), ct);
            var auth = await client.Authentication.BeginAuthSessionViaCredentialsAsync(new AuthSessionDetails
            {
                Username = username,
                Password = password,
                IsPersistentSession = true,
                Authenticator = new UserConsoleAuthenticator(),
            });
            var result = await auth.PollingWaitForResultAsync(ct);
            SessionStore.Save(sessionDir, new SteamSession(result.AccountName, result.RefreshToken));
            log.WriteLine($"Saved a login for {result.AccountName} to {Path.Combine(sessionDir, SessionStore.FileName)}");
            return ExitCodes.Completed;
        }
        catch (AuthenticationException e)
        {
            log.WriteLine($"Steam refused the login: {e.Result}");
            return ExitCodes.LoginRefused;
        }
        finally
        {
            client.Disconnect();
            pumpStop.Cancel();
            await pump;
        }
    }

    private static string ReadSecret()
    {
        var secret = new StringBuilder();
        ConsoleKeyInfo key;
        while ((key = Console.ReadKey(intercept: true)).Key != ConsoleKey.Enter)
        {
            if (key.Key == ConsoleKey.Backspace)
            {
                if (secret.Length > 0)
                {
                    secret.Length--;
                }
            }
            else
            {
                secret.Append(key.KeyChar);
            }
        }
        Console.Error.WriteLine();
        return secret.ToString();
    }
}
