# ISP information in disable notifications

ISP information is requested only after a user is successfully disabled or their
subscription is revoked and disabled, with a Telegram forum group configured.
Warning scans, active-user displays and
12-hour or 48-hour IP-history reports do not request ISP information.

The disable notification shows each IP's ISP and country alongside its activity
duration and scan count. Each IP is queried separately, including IPs in the same
subnet. Results are not cached in memory or the database. Repeated disables query
the IPs again. Requests run with bounded concurrency and an eight-second total
deadline; a failed or slow lookup displays `Unknown ISP` and does not prevent the
disable notification.

Configure the ipinfo token through Telegram's `/set_ipinfo_token` command or the
IPInfo settings screen. With a token, notices use the
[IPinfo Lite API](https://ipinfo.io/developers/lite-api), which accepts current and
existing legacy tokens. Each IP uses one request to `https://api.ipinfo.io/lite/{ip}`;
the ISP name and country code appear in the notice. Without a token, notices use
ip-api.com. Country filtering still uses its own country providers and caches,
without requesting ISP information or contacting ipinfo.io.

Subnet IP Grouping remains available for `/24` device counting. The previous
`/16` mode depended on ISP information and is retired. Existing `/16` settings
automatically use `/24`, so IPs in different `/24` blocks remain separate unless
another configured counting rule combines them. The old subnet-mode button also
retains `/24`. High Trust grouping continues to use IP/inbound activity and
punishment history; ISP names no longer influence its score.

Database migration `009_drop_isp_cache` removes the unused `subnet_isp` table and
its lookup results. Downgrading recreates the empty cache schema. User records,
limits and punishment history are preserved.
