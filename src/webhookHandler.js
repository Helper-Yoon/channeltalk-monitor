const crypto = require('crypto');

function setupWebhook(app, redisClient, io) {
  const WEBHOOK_TOKEN = '80ab2d11835f44b89010c8efa5eec4b4';
  
  // 웹훅 엔드포인트
  app.post('/webhook/channel-talk', (req, res) => {
    try {
      // 토큰 검증 (선택적)
      const authHeader = req.headers['x-channel-token'];
      if (authHeader && authHeader !== WEBHOOK_TOKEN) {
        return res.status(401).send('Unauthorized');
      }
      
      const body = JSON.parse(req.body);
      const { event, type, entity, refers } = body;
      
      // 새 메시지 이벤트 처리
      if (type === 'message' && entity.personType === 'user') {
        handleNewCustomerMessage(entity, refers);
      }
      
      // 새 채팅 생성 이벤트
      if (type === 'userChat' && entity.state === 'opened') {
        handleNewChat(entity, refers);
      }
      
      res.status(200).send('OK');
    } catch (error) {
      console.error('Webhook error:', error);
      res.status(500).send('Internal Server Error');
    }
  });
  
  async function handleNewCustomerMessage(message, refers) {
    const chatId = message.chatId || message.chatKey;
    
    // Redis 업데이트
    const consultationData = {
      id: chatId,
      customerMessage: message.plainText || '',
      frontUpdatedAt: message.createdAt,
      waitTime: Math.floor((Date.now() - message.createdAt) / 1000 / 60)
    };
    
    await redisClient.hSet(`consultation:${chatId}`, consultationData);
    await redisClient.zAdd('consultations:waiting', {
      score: message.createdAt,
      value: chatId
    }); 
    
    // 실시간 업데이트 브로드캐스트
    io.to('dashboard').emit('consultation:updated', {
      id: chatId,
      ...consultationData
    });
    
    console.log(`New customer message in chat ${chatId}`);
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
      
      await redisClient.hSet(`consultation:${userChat.id}`, consultationData);
      await redisClient.zAdd('consultations:waiting', {
        score: userChat.createdAt,
        value: userChat.id
      }); 
      
      io.to('dashboard').emit('consultation:new', consultationData);
      
      console.log(`New unassigned chat: ${userChat.id}`);
    }
  }
}

module.exports = { setupWebhook };
