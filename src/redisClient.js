const { createClient } = require('redis');

async function initializeRedis() {
  const client = createClient({
    url: 'redis://red-d2ct46buibrs738rintg:6379',
    socket: {
      connectTimeout: 10000,
      reconnectStrategy: (retries) => {
        console.log(`Redis reconnection attempt ${retries}`);
        return Math.min(retries * 100, 3000);
      }
    }
  });

  client.on('error', (error) => {
    console.error('Redis error:', error);
  });

  client.on('connect', () => {
    console.log('Redis connected successfully');
  }); 

  await client.connect();
  return client;
}

module.exports = { initializeRedis };
