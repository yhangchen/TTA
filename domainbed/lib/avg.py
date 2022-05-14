class AvgMeter:
    def __init__(self, data=None, gamma=0.99):
        if data is None:
            self.data = 0.0
            self.count = 0
        else:
            self.data = data
            self.count = 1
        self.gamma = gamma
    
    def update(self, data, count=1):
        self.count += count
        self.data = self.data * self.gamma + data

    def get(self):
        return self.data / max(self.count, 1)
