class AvgMeter:
    def __init__(self, data=None):
        if data is None:
            self.data = 0.0
            self.count = 0
        else:
            self.data = data
            self.count = 1
    
    def update(self, data, count=1):
        self.data += data
        self.count += count
    
    def get(self):
        return self.data / max(self.count, 1)
