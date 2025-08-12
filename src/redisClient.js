import { createClient } from 'redis';

export async function initializeRedis() {
  const redisUrl = process.env.REDIS_URL || 'redis://red-d2ct46buibrs738rintg:6379';
  
  const client = createClient({
    url: redisUrl,
    socket: {
      connectTimeout: 10000,
      reconnectStrategy: (retries) => {
        console.log(`Redis reconnection attempt ${retries}`);
        if (retries > 10) {
          return new Error('Too many retries');
        }
        return Math.min(retries * 100, 3000);
      }
    }
  });

  client.on('error', (error) => {
    console.error('Redis Client Error:', error);
  });

  client.on('connect', () => {
    console.log('✅ Redis connected successfully');
  });

  client.on('ready', () => {
    console.log('✅ Redis client ready');
  });

  await client.connect();
  return client;
}
