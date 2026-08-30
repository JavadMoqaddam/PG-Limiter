"""
ISP Detection Module
This module provides functionality to detect ISP information for IP addresses.
Lookups are served from a bounded in-process cache first, then from the
database-backed subnet cache, and only then from the external APIs.
"""

import asyncio
from typing import Dict, Optional, Tuple
import httpx
from utils.logs import logger

# Try to import database-backed subnet cache
try:
    from utils.db_handler import get_db_subnet_cache, DB_AVAILABLE
except ImportError:
    DB_AVAILABLE = False
    get_db_subnet_cache = None


def _default_isp_info(ip: str) -> Dict[str, str]:
    """Return default ISP info for an IP"""
    return {"ip": ip, "isp": "Unknown ISP", "country": "Unknown", "city": "Unknown", "region": "Unknown"}


class ISPDetector:
    """
    A class to detect ISP information for IP addresses
    """
    
    def __init__(self, token: Optional[str] = None, use_fallback_only: bool = False, use_db_cache: bool = True):
        """
        Initialize the ISP detector with an optional ipinfo token
        
        Args:
            token (Optional[str]): ipinfo.io API token (optional for basic usage)
            use_fallback_only (bool): If True, use only ip-api.com instead of ipinfo.io
            use_db_cache (bool): If True, use database-backed subnet cache for persistence
        """
        self.token = token
        # Auto-enable fallback if no token provided (ipinfo.io rate limits quickly without token)
        self.use_fallback_only = use_fallback_only or (not token)
        self.use_db_cache = use_db_cache and DB_AVAILABLE
        self.cache: Dict[str, Dict[str, str]] = {}  # Bounded cache to avoid memory leak
        self._max_cache_size = 5000  # Cap cache entries for RAM safety
        self._background_tasks: set[asyncio.Task] = set()  # Retain references to avoid GC cleanup
        self.rate_limit_delay = 1  # 1 second delay between requests
        self.last_request_time = 0
        self.rate_limited = False  # Track if we're rate limited
        self._client: Optional[httpx.AsyncClient] = None  # Shared httpx AsyncClient
        self._db_cache = get_db_subnet_cache() if self.use_db_cache else None
        
        if self.use_db_cache:
            logger.info("ISPDetector initialized with database-backed subnet cache")
        if self.use_fallback_only:
            logger.info("ISPDetector using ip-api.com (fallback mode - no token configured)")
        elif token:
            logger.info(f"ISPDetector initialized with token: {token[:20]}...")

    def _set_cache(self, key: str, value: Dict[str, str]) -> None:
        """Store ISP entry in RAM with LRU-style eviction to prevent memory leak."""
        if len(self.cache) >= self._max_cache_size:
            # Evict oldest 10% of entries
            evict_count = max(1, self._max_cache_size // 10)
            keys_to_evict = list(self.cache.keys())[:evict_count]
            for k in keys_to_evict:
                self.cache.pop(k, None)
        self.cache[key] = value

    def _create_background_task(self, coro) -> asyncio.Task:
        """Create a background task and hold a strong reference until completed."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task
    
    def update_token(self, token: Optional[str]):
        """Update ipinfo token dynamically"""
        if token:
            self.token = token
            self.use_fallback_only = False
            self.rate_limited = False
            logger.info(f"🔑 ISPDetector token updated: {token[:20]}...")
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the shared httpx AsyncClient"""
        if self._client is None or self._client.is_closed:
            # Create client with connection limits
            limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
            self._client = httpx.AsyncClient(limits=limits, timeout=8.0)
        return self._client
    
    # Backwards compatibility alias
    _get_session = _get_client
    
    async def close(self):
        """Close the httpx AsyncClient"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
    
    async def get_isp_info(self, ip: str) -> Dict[str, str]:
        """
        Get ISP information for a given IP address.
        Checks Redis cache first, then memory, then database, finally API.
        
        Args:
            ip (str): IP address to lookup
            
        Returns:
            Dict[str, str]: Dictionary containing ISP information
        """
        # Check in-memory cache first (instant)
        if ip in self.cache:
            return self.cache[ip]

        # Check database cache (by subnet) if enabled
        if self._db_cache:
            try:
                async with asyncio.timeout(3):  # 3 second DB cache timeout
                    cached = await self._db_cache.get_cached_isp(ip)
                    if cached:
                        await self._cache_isp_result(ip, cached)
                        logger.debug(f"ISP cache hit for {ip} (subnet cache)")
                        return cached
            except asyncio.TimeoutError:
                logger.warning(f"DB cache timeout for {ip}")
            except Exception as e:
                logger.warning(f"Database cache lookup failed: {e}")
        
        # If use_fallback_only is enabled, skip ipinfo.io and use ip-api.com directly
        if self.use_fallback_only:
            result = await self._get_isp_fallback(ip)
            await self._cache_isp_result(ip, result)
            return result
        
        # If we're rate limited, return default info immediately
        if self.rate_limited:
            return {
                "ip": ip,
                "isp": "Unknown ISP",
                "country": "Unknown",
                "city": "Unknown",
                "region": "Unknown"
            }
            
        # Rate limiting
        current_time = asyncio.get_event_loop().time()
        if current_time - self.last_request_time < self.rate_limit_delay:
            await asyncio.sleep(self.rate_limit_delay - (current_time - self.last_request_time))
        
        try:
            # Try ipinfo.io API first
            url = f"https://ipinfo.io/{ip}/json"
            headers = {}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
                logger.debug(f"ISP lookup for {ip} with token")
            else:
                logger.debug(f"ISP lookup for {ip} without token")
            
            client = await self._get_client()
            response = await client.get(url, headers=headers, timeout=8.0)
            if response.status_code == 200:
                data = response.json()
                # Prefer as_domain, fallback to as_name, then org
                isp_name = data.get("as_domain") or data.get("as_name") or data.get("org", "Unknown ISP")
                logger.debug(f"ISP detected for {ip}: {isp_name}")
                isp_info = {
                    "ip": ip,
                    "isp": isp_name,
                    "country": data.get("country", "Unknown"),
                    "city": data.get("city", "Unknown"),
                    "region": data.get("region", "Unknown")
                }
                self._set_cache(ip, isp_info)
                self.last_request_time = asyncio.get_event_loop().time()
                # Save to all caches (Redis + database) with protected task reference
                self._create_background_task(self._cache_isp_result(ip, isp_info))
                return isp_info
            elif response.status_code == 429:
                # Rate limited - set flag and return default
                self.rate_limited = True
                logger.warning(f"ISP detection rate limited for {ip}")
            elif response.status_code == 403:
                # Forbidden - try fallback API
                logger.warning(f"ipinfo.io returned 403 for {ip}, trying fallback API...")
                result = await self._get_isp_fallback(ip)
                self._create_background_task(self._cache_isp_result(ip, result))
                return result
            else:
                response_text = response.text
                logger.warning(f"Failed to get ISP info for {ip}: HTTP {response.status_code} - {response_text[:100]}")
                
        except (httpx.TimeoutException, asyncio.TimeoutError):
            logger.warning(f"⏱️ Timeout getting ISP info for {ip}, trying fallback...")
            # Try fallback on timeout - don't wait for cache
            result = await self._get_isp_fallback(ip)
            self._create_background_task(self._cache_isp_result(ip, result))
            return result
        except Exception as e:
            logger.warning(f"❌ Error getting ISP info for {ip}: {type(e).__name__}, trying fallback...")
            # Try fallback on any error - don't wait for cache
            result = await self._get_isp_fallback(ip)
            self._create_background_task(self._cache_isp_result(ip, result))
            return result
        
        # Return default info if lookup fails
        default_info = {
            "ip": ip,
            "isp": "Unknown ISP",
            "country": "Unknown",
            "city": "Unknown",
            "region": "Unknown"
        }
        self._set_cache(ip, default_info)
        return default_info
    
    async def _save_to_db_cache(self, ip: str, isp_info: Dict[str, str]):
        """Save ISP info to database cache (by subnet)"""
        if self._db_cache and isp_info.get("isp") != "Unknown ISP":
            try:
                async with asyncio.timeout(3):  # 3 second DB timeout
                    await self._db_cache.cache_isp(
                        ip=ip,
                        isp_name=isp_info.get("isp", "Unknown ISP"),
                        country=isp_info.get("country"),
                        city=isp_info.get("city"),
                        region=isp_info.get("region"),
                    )
            except asyncio.TimeoutError:
                logger.debug(f"DB cache save timeout for {ip}")
            except Exception as e:
                logger.warning(f"Failed to save ISP to database cache: {e}")
    
    async def _cache_isp_result(self, ip: str, isp_info: Dict[str, str]):
        """Persist an ISP result to the subnet cache in the database."""
        if isp_info.get("isp") == "Unknown ISP":
            return

        await self._save_to_db_cache(ip, isp_info)
    
    async def _get_isp_fallback(self, ip: str) -> Dict[str, str]:
        """
        Fallback method to get ISP info using alternative free APIs
        
        Args:
            ip (str): IP address to lookup
            
        Returns:
            Dict[str, str]: ISP information dictionary
        """
        # Try ip-api.com (free, no token needed, 45 req/min)
        try:
            url = f"http://ip-api.com/json/{ip}?fields=status,message,country,countryCode,region,regionName,city,isp,org,as,asname"
            
            client = await self._get_client()
            response = await client.get(url, timeout=8.0)
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "success":
                    # Prefer asname, fallback to isp, then org
                    isp_name = data.get("asname") or data.get("isp") or data.get("org", "Unknown ISP")
                    isp_info = {
                        "ip": ip,
                        "isp": isp_name,
                        "country": data.get("countryCode", "Unknown"),
                        "city": data.get("city", "Unknown"),
                        "region": data.get("regionName", "Unknown")
                    }
                    logger.debug(f"✓ Fallback API success for {ip}: {isp_info['isp']}")
                    self._set_cache(ip, isp_info)
                    return isp_info
                else:
                    logger.warning(f"Fallback API returned failure for {ip}: {data.get('message', 'unknown')}")
            elif response.status_code == 429:
                logger.warning(f"Fallback API rate limited for {ip}")
            else:
                logger.warning(f"Fallback API HTTP {response.status_code} for {ip}")
        except (httpx.TimeoutException, asyncio.TimeoutError):
            logger.warning(f"Fallback API timeout for {ip}")
        except Exception as e:
            logger.warning(f"Fallback API error for {ip}: {type(e).__name__}: {str(e)[:100]}")
        
        # If all fails, return default
        default_info = {
            "ip": ip,
            "isp": "Unknown ISP",
            "country": "Unknown",
            "city": "Unknown",
            "region": "Unknown"
        }
        self._set_cache(ip, default_info)
        return default_info
    
    async def get_multiple_isp_info(self, ips: list[str], timeout: float = 8.0) -> Dict[str, Dict[str, str]]:
        """
        Get ISP information for multiple IP addresses using Subnet Aggregation (/24)
        and multi-level caching (Memory -> DB Subnet -> API).
        """
        if not ips:
            return {}

        results = {}
        uncached_subnets: Dict[str, list[str]] = {}

        # Helper to extract /24 subnet key
        def get_subnet(ip_str: str) -> str:
            if "." in ip_str:
                return f"{ip_str.rsplit('.', 1)[0]}.0"
            return ip_str

        # Step 1: Check Memory Cache (Instant O(1))
        for ip in ips:
            if ip in self.cache:
                results[ip] = self.cache[ip]
                continue
            
            subnet_key = get_subnet(ip)
            if subnet_key in self.cache:
                info = dict(self.cache[subnet_key])
                info["ip"] = ip
                self._set_cache(ip, info)
                results[ip] = info
                continue
            
            # Group uncached IPs by subnet key
            if subnet_key not in uncached_subnets:
                uncached_subnets[subnet_key] = []
            uncached_subnets[subnet_key].append(ip)

        # Step 2: Check DB Subnet Cache for uncached subnets
        if self._db_cache and uncached_subnets:
            subnets_to_query = list(uncached_subnets.keys())
            for subnet_key in subnets_to_query:
                sample_ip = uncached_subnets[subnet_key][0]
                try:
                    db_cached = await self._db_cache.get_cached_isp(sample_ip)
                    if db_cached and db_cached.get("isp") != "Unknown ISP":
                        self._set_cache(subnet_key, db_cached)
                        for ip in uncached_subnets[subnet_key]:
                            info = dict(db_cached)
                            info["ip"] = ip
                            self._set_cache(ip, info)
                            results[ip] = info
                        del uncached_subnets[subnet_key]
                except Exception:
                    pass

        if not uncached_subnets:
            return {ip: results.get(ip, _default_isp_info(ip)) for ip in ips}

        # Step 3: Query external API for remaining uncached subnets (Rate Limited with Semaphore)
        total_uncached_ips = sum(len(v) for v in uncached_subnets.values())
        logger.info(f"🔍 ISP lookup: Aggregated {total_uncached_ips} IPs into {len(uncached_subnets)} /24 subnets")
        semaphore = asyncio.Semaphore(3)

        client = await self._get_client()

        async def lookup_subnet(subnet_key: str, sample_ip: str) -> Tuple[str, Dict[str, str]]:
            async with semaphore:
                try:
                    url = f"http://ip-api.com/json/{sample_ip}?fields=status,country,countryCode,regionName,city,isp,org,asname"
                    response = await client.get(url, timeout=4.0)
                    if response.status_code == 200:
                        data = response.json()
                        if data.get("status") == "success":
                            isp_name = data.get("asname") or data.get("isp") or data.get("org") or "Unknown ISP"
                            info = {
                                "ip": sample_ip,
                                "isp": isp_name,
                                "country": data.get("countryCode", "Unknown"),
                                "city": data.get("city", "Unknown"),
                                "region": data.get("regionName", "Unknown")
                            }
                            self._set_cache(subnet_key, info)
                            self._create_background_task(self._save_to_db_cache(sample_ip, info))
                            return subnet_key, info
                except Exception:
                    pass
                
                default_res = _default_isp_info(sample_ip)
                self._set_cache(subnet_key, default_res)
                return subnet_key, default_res

        try:
            async with asyncio.timeout(timeout):
                tasks = [lookup_subnet(s_key, ips_list[0]) for s_key, ips_list in uncached_subnets.items()]
                api_results = await asyncio.gather(*tasks, return_exceptions=True)

                for res in api_results:
                    if isinstance(res, tuple):
                        s_key, info = res
                        for ip in uncached_subnets.get(s_key, []):
                            ip_info = dict(info)
                            ip_info["ip"] = ip
                            self._set_cache(ip, ip_info)
                            results[ip] = ip_info
        except asyncio.TimeoutError:
            logger.warning(f"⏱️ ISP subnet lookup timeout ({timeout}s)")

        # Step 4: Ensure all requested IPs have results and are cached to avoid repeated retries
        for ip in ips:
            if ip not in results or not results[ip]:
                def_info = _default_isp_info(ip)
                self._set_cache(ip, def_info)
                results[ip] = def_info

        return results
    
    def format_ip_with_isp(self, ip: str, isp_info: Dict[str, str]) -> str:
        """
        Format IP address with ISP information
        
        Args:
            ip (str): IP address
            isp_info (Dict[str, str]): ISP information dictionary
            
        Returns:
            str: Formatted string with IP and ISP info
        """
        isp = isp_info.get("isp", "Unknown ISP")
        country = isp_info.get("country", "Unknown")
        
        # If ISP information is unavailable or unknown, just return the IP
        if isp == "Unknown ISP" and country == "Unknown":
            return ip
        
        # Clean up ISP name (remove common prefixes)
        if isp.startswith("AS"):
            # Remove AS number prefix
            parts = isp.split(" ", 1)
            if len(parts) > 1:
                isp = parts[1]
        
        return f"{ip} ({isp}, {country})"
    
    def clear_cache(self):
        """Clear the ISP cache"""
        self.cache.clear()
