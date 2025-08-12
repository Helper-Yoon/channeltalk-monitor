// channelAPI.js에 추가
async quickSync() {
  try {
    console.log('=== Quick sync starting ===');
    this.apiCallCount = 0;
    
    // 매니저 정보 로드
    if (Object.keys(this.managers).length === 0) {
      await this.loadManagers();
    }
    
    // 최근 500개만 빠르게 가져오기
    const data = await this.makeRequest('/user-chats?state=opened&limit=500&sortOrder=desc');
    const userChats = data.userChats || [];
    
    console.log(`Quick sync: Found ${userChats.length} recent chats`);
    
    // 미답변 상담만 빠르게 처리
    const unansweredChats = [];
    
    for (const chat of userChats) {
      try {
        const messagesData = await this.makeRequest(`/user-chats/${chat.id}/messages?sortOrder=desc&limit=3`);
        const messages = messagesData.messages || [];
        
        const lastCustomerMessage = messages.find(m => m.personType === 'user');
        const lastManagerMessage = messages.find(m => m.personType === 'manager');
        
        if (lastCustomerMessage && (!lastManagerMessage || lastCustomerMessage.createdAt > lastManagerMessage.createdAt)) {
          // 담당자 정보
          let counselorName = '미배정';
          let teamName = '없음';
          
          if (chat.assigneeId && this.managers[chat.assigneeId]) {
            const assignee = this.managers[chat.assigneeId];
            counselorName = assignee.name || assignee.displayName || '미배정';
            teamName = this.findTeamByName(counselorName);
          }
          
          // 고객 정보
          let customerName = chat.name || '익명';
          
          const consultationData = {
            id: String(chat.id),
            customerName: String(customerName),
            customerMessage: String(lastCustomerMessage.plainText || lastCustomerMessage.message || ''),
            team: String(teamName),
            counselor: String(counselorName),
            waitTime: String(this.calculateWaitTime(lastCustomerMessage.createdAt)),
            createdAt: String(chat.createdAt),
            frontUpdatedAt: String(lastCustomerMessage.createdAt),
            chatUrl: `https://desk.channel.io/#/channels/197228/user_chats/${chat.id}`
          };
          
          unansweredChats.push(consultationData);
          
          // Redis에 저장
          const redisData = Object.entries(consultationData)
            .map(([key, value]) => [key, String(value)])
            .flat();
          
          await this.redis.hSet(`consultation:${chat.id}`, redisData);
          await this.redis.zAdd('consultations:waiting', {
            score: lastCustomerMessage.createdAt,
            value: String(chat.id)
          });
          await this.redis.expire(`consultation:${chat.id}`, 86400);
        }
      } catch (error) {
        console.error(`Error in quick sync for chat ${chat.id}:`, error.message);
      }
    }
    
    console.log(`=== Quick sync complete: ${unansweredChats.length} unanswered chats ===`);
    
    // 정렬
    unansweredChats.sort((a, b) => {
      const waitTimeDiff = parseInt(b.waitTime || 0) - parseInt(a.waitTime || 0);
      if (waitTimeDiff !== 0) return waitTimeDiff;
      return 0;
    });
    
    // 대시보드 업데이트
    this.io.to('dashboard').emit('dashboard:update', unansweredChats);
    
    // 첫 동기화 시간 설정
    this.lastSyncTime = Date.now();
    
  } catch (error) {
    console.error('Quick sync error:', error);
  }
}
