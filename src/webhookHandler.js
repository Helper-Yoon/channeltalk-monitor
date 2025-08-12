export function setupWebhook(app, redisClient, io, channelService) {
  const WEBHOOK_TOKEN = process.env.WEBHOOK_TOKEN || '80ab2d11835f44b89010c8efa5eec4b4';
  
  app.post('/webhook/channel-talk', async (req, res) => {
    try {
      // 토큰 검증
      const authHeader = req.headers['x-channel-token'];
      if (authHeader && authHeader !== WEBHOOK_TOKEN) {
        return res.status(401).send('Unauthorized');
      }
      
      const body = JSON.parse(req.body);
      const { type, entity, refers } = body;
      
      console.log(`Webhook received: ${type}`);
      
      // 새 메시지 이벤트
      if (type === 'message' && entity.personType === 'user') {
        await handleNewCustomerMessage(entity, refers);
      }
      
      // 새 채팅 생성
      if (type === 'userChat' && entity.state === 'opened') {
        await handleNewChat(entity, refers);
      }
      
      res.status(200).send('OK');
    } catch (error) {
      console.error('Webhook error:', error);
      res.status(500).send('Internal Server Error');
    }
  });
  
  async function handleNewCustomerMessage(message, refers) {
    const chatId = message.chatId || message.chatKey;
    
    const consultationData = {
      id: chatId,
      customerMessage: message.plainText || '',
      frontUpdatedAt: message.createdAt,
      waitTime: Math.floor((Date.now() - message.createdAt) / 60000)
    };
    
    await redisClient.hSet(
      `consultation:${chatId}`,
      Object.entries(consultationData).map(([k, v]) => [k, String(v)]).flat()
    );
    
    await redisClient.zAdd('consultations:waiting', {
      score: message.createdAt,
      value: String(chatId)
    });
    
    io.to('dashboard').emit('consultation:updated', consultationData);
    console.log(`New message in chat ${chatId}`);
  }
  
  async function handleNewChat(userChat, refers) {
    if (!userChat.assigneeId) {
      const consultationData = {
        id: userChat.id,
        customerName: refers?.user?.name || '익명',
        team: '미배정',
        counselor: null,
        createdAt: userChat.createdAt,
        frontUpdatedAt: userChat.createdAt,
        waitTime: 0,
        chatUrl: `https://desk.channel.io/#/channels/197228/user_chats/${userChat.id}`
      };
      
      await redisClient.hSet(
        `consultation:${userChat.id}`,
        Object.entries(consultationData).map(([k, v]) => [k, String(v)]).flat()
      );
      
      await redisClient.zAdd('consultations:waiting', {
        score: userChat.createdAt,
        value: String(userChat.id)
      });
      
      io.to('dashboard').emit('consultation:new', consultationData);
      console.log(`New chat: ${userChat.id}`);
    }
  }
}
